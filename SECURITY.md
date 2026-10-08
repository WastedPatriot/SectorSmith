# Security

SectorSmith reads and writes raw disks and can link machines over the network, so security reports are taken seriously.

* **Report privately** via GitHub → Security → *Report a vulnerability* (please don't open a public issue).
* **Link security model:** TLS 1.2+ with a one-time self-signed certificate whose SHA-256 fingerprint is pinned inside the copy-paste command, plus a random 128-bit token. The agent executable is downloaded from the controller and verified against a SHA-256 in the command before it runs. USB/listen mode uses an 8-character pairing code and shows a 6-character check code on both screens.
* Every push is scanned by **CodeQL**; dependencies are watched by **Dependabot**.
