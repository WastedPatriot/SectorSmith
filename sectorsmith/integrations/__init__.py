"""Optional, read-mostly links to other tools an MSP already runs. Each one is off until it is configured.

screenconnect.py opens a machine in ScreenConnect (ConnectWise Control) and reads this PC's installed client.
ad.py looks a user up in this PC's Active Directory domain (ADSI through PowerShell, no stored credentials).
dpapi.py keeps any secret a future integration needs, encrypted with Windows DPAPI."""
