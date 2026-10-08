import sys

if "--agent" in sys.argv:  # headless-capable agent mode: no GUI toolkit imports needed
    from .util import relaunch_as_admin
    if "--no-elevate" not in sys.argv and relaunch_as_admin():
        sys.exit(0)
    from .link.agent import main as agent_main
    sys.exit(agent_main(sys.argv[1:]))

from .app import main  # noqa: E402

if __name__ == "__main__":
    main()
