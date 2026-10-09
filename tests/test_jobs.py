"""Job history store: start and end writes, merging, a corrupt or torn line, interrupted jobs, the cap and
rotation, filters, the date range and CSV export. Needs no test disk and no admin rights.

Usage: python tests/test_jobs.py [workdir]
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sectorsmith import jobs as J  # noqa: E402

W = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="ss_jobs_")
shutil.rmtree(W, ignore_errors=True)
os.makedirs(W, exist_ok=True)
OK = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    OK.append(bool(cond))


path = os.path.join(W, "jobs.jsonl")
st = J.JobStore(path)
check(st.load() == [], "missing file loads as empty history")

a = st.start(task="Erase and certify", title="Wipe", client="Acme Legal", ticket="48213", technician="Dom",
             machine="This PC", detail="Disk 2")
check(a["result"] == "Running" and a["ended"] is None and len(a["id"]) == 16, "start record")
with open(path, encoding="utf-8") as fh:
    check(len(fh.read().splitlines()) == 1, "written when the job starts")
check(st.load()[0]["result"] == "Running", "a job this process is running still reads as Running")
st.finish(a, "Done", report=os.path.join(W, "cert.html"))
b = st.start(task="Health check", title="Health", client="", ticket="", technician="Dom", machine="This PC",
             detail="Disk 1")
st.finish(b, "Failed")
c = st.start(task="Image and clone", title="Copy", client="Bright Dental", ticket="77", technician="Sam",
             machine="This PC", detail="Disk 3 to backup.vhd")
st.finish(c, "Cancelled")
recs = st.load()
check([r["task"] for r in recs] == ["Erase and certify", "Health check", "Image and clone"], "merged, oldest first")
check(recs[0]["result"] == "Done" and recs[0]["report"].endswith("cert.html") and recs[0]["ended"], "end merged")
st.attach(c, os.path.join(W, "clone.txt"))
check(st.load()[2]["report"].endswith("clone.txt"), "report attached after the job ended")

# a job that never ended (SectorSmith closed mid-job) reads back as Interrupted in the next session
d = st.start(task="Shred files", title="Shred", client="Acme Legal", ticket="", technician="Dom",
             machine="This PC", detail="3 files")
again = J.JobStore(path)
check(again.load()[-1]["result"] == "Interrupted", "unfinished job from an earlier session is Interrupted")
st.finish(d, "Done")

# a corrupt line, a torn last line and junk rows are skipped; the next append starts on a fresh line
with open(path, "a", encoding="utf-8") as fh:
    fh.write("this is not json\n[1, 2]\n{\"no_id\": true}\n{\"id\": \"torn\", \"task\": \"Half")
n_before = len(st.load())
e = st.start(task="Migrate user", title="Migration", client="Acme Legal", ticket="9", technician="Dom",
             machine="This PC", detail="old to new")
st.finish(e, "Done")
recs = st.load()
check(len(recs) == n_before + 1 and recs[-1]["task"] == "Migrate user", "survives a corrupt and a torn line")
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()
torn = [line for line in lines if "torn" in line]
check(len(torn) == 1 and torn[0].endswith('"Half') and json.loads(lines[-1])["task"] == "Migrate user",
      "append after a torn line starts a new line")

# a record with a broken start time is dropped, not fatal
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"id": "badtime", "task": "x", "started": "soon"}) + "\n")
check(all(r["id"] != "badtime" for r in st.load()), "bad start time skipped")

# concurrent writers: every line stays whole
p2 = os.path.join(W, "threads.jsonl")
s2 = J.JobStore(p2)


def burst():
    for _ in range(40):
        s2.finish(s2.start(task="t", detail="x" * 300), "Done")


ts = [threading.Thread(target=burst) for _ in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
with open(p2, encoding="utf-8") as fh:
    rows = [json.loads(line) for line in fh]
check(len(rows) == 320 and len(s2.load()) == 160, "threads append whole lines")

# cap and rotation: the newest CAP jobs stay, older ones move to jobs.1.jsonl
p3 = os.path.join(W, "cap.jsonl")
s3 = J.JobStore(p3, cap=20)
for i in range(60):
    s3.finish(s3.start(task=f"job {i}", started=1_700_000_000 + i), "Done")
recs = s3.load()
check(len(recs) <= 2 * 20 + 50 and recs[-1]["task"] == "job 59", "history capped, newest kept")
for i in range(60, 100):
    s3.finish(s3.start(task=f"job {i}", started=1_700_000_000 + i), "Done")
recs = s3.load()
check(20 <= len(recs) <= 46 and recs[-1]["task"] == "job 99", f"compacted to the cap ({len(recs)} jobs)")
check(os.path.exists(s3.archive) and len(J.JobStore(s3.archive).load()) > 0, "older jobs rotated to jobs.1.jsonl")
check(not os.path.exists(p3 + ".tmp"), "no temp file left behind")
olds = J.JobStore(s3.archive).load()
check({r["task"] for r in olds}.isdisjoint({r["task"] for r in recs}), "archive and live file don't overlap")

# filters
recs = J.JobStore(path).load()
check([r["task"] for r in J.filter_jobs(recs, client="Acme Legal")] ==
      ["Erase and certify", "Shred files", "Migrate user"], "filter by client")
check([r["task"] for r in J.filter_jobs(recs, client="")] == ["Health check"], "filter: no client")
check([r["task"] for r in J.filter_jobs(recs, result="Cancelled")] == ["Image and clone"], "filter by result")
check([r["task"] for r in J.filter_jobs(recs, text="disk 3 BACKUP")] == ["Image and clone"], "text: all words")
check([r["task"] for r in J.filter_jobs(recs, text="48213")] == ["Erase and certify"], "text matches ticket")
check(J.filter_jobs(recs, text="nothing-like-this") == [], "text with no match")
now = time.time()
check(len(J.filter_jobs(recs, since=now - 3600)) == len(recs) and J.filter_jobs(recs, until=now - 3600) == [],
      "filter by time")
since, until = J.day_range(time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d"))
check(until - since in (86400, 82800, 90000) and since <= now < until, "day range covers today")
check(J.day_range("", "") == (None, None), "blank range")
try:
    J.day_range("2026-13-40")
    check(False, "bad date rejected")
except ValueError:
    check(True, "bad date rejected")
old = [{"id": "o", "task": "Old", "started": J.day_range("2024-03-01")[0] + 3600, "result": "Done"}]
check(len(J.filter_jobs(old, *[None, None], *J.day_range("2024-03-01", "2024-03-01"))) == 1
      and J.filter_jobs(old, *[None, None], *J.day_range("2024-03-02", "")) == [], "custom range bounds")

# CSV export: header, values, formula-looking cells neutralised
csvp = os.path.join(W, "jobs.csv")
evil = dict(recs[0], detail="=HYPERLINK(\"x\")", client="+Acme")
n = J.export_csv([evil] + recs[1:], csvp)
with open(csvp, encoding="utf-8-sig", newline="") as fh:
    rows = list(csv.reader(fh))
check(n == len(recs) and rows[0][:3] == ["Started", "Ended", "Task"] and len(rows) == len(recs) + 1, "CSV rows")
check(rows[1][3].startswith("'=") and rows[1][4] == "'+Acme", "CSV formula injection guarded")
check(rows[1][7] == "This PC" and rows[1][8] == "Done" and rows[1][9].endswith("cert.html")
      and rows[1][0][:4].isdigit(), "CSV values")

# write failures are logged, never raised
bad = J.JobStore(os.path.join(W, "jobs.jsonl", "nested", "x.jsonl"))
r = bad.start(task="t")
bad.finish(r, "Done")
check(r["result"] == "Done", "unwritable history doesn't break the job")

shutil.rmtree(W, ignore_errors=True)
print(f"\n{sum(OK)}/{len(OK)} job history checks passed")
sys.exit(0 if all(OK) else 1)
