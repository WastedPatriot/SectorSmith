# Third-party notices

SectorSmith is proprietary software (see [LICENSE](LICENSE)). Release builds
bundle the open source components below. Each one stays under its own licence,
and nothing in the SectorSmith licence restricts your rights under those terms.

| Component | Used for | Licence |
|---|---|---|
| [Python](https://www.python.org/) | Runtime bundled in the .exe | PSF License |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | UI widgets | MIT (5.x), CC0 (6.x) |
| [darkdetect](https://github.com/albertosottile/darkdetect) | Follows the Windows light/dark setting (CustomTkinter dependency) | BSD-3-Clause |
| [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) | Drag and drop | MIT |
| [tkdnd](https://github.com/petasis/tkdnd) | Native drag and drop library shipped inside tkinterdnd2 | BSD-style (Tcl/Tk licence) |
| [Tcl/Tk](https://www.tcl.tk/) | Windowing toolkit used by tkinter | Tcl/Tk licence (BSD-style) |
| [cryptography](https://github.com/pyca/cryptography) | TLS certificates for SectorSmith Link | Apache-2.0 or BSD-3-Clause |
| [OpenSSL](https://www.openssl.org/) | Crypto library used by Python and cryptography | Apache-2.0 |
| [olefile](https://github.com/decalage2/olefile) | Reading MSI installers | BSD-2-Clause |
| [PyInstaller](https://pyinstaller.org/) bootloader | Single-file .exe packaging | GPL-2.0 with the PyInstaller bootloader exception, which allows use in proprietary programs |

The full licence texts ship with each package and are available at the links
above.
