# Third-party components

The fishing helper grew out of an experimental local adaptation of
[lanqvist/AHelperW](https://github.com/lanqvist/AHelperW) (revision
38fa8a43ed2f08c39645d1b6ff339151aa5e1a81). This notice grants no new license to that code.

The released program (installer and portable zip) bundles:

| Component | License | Use |
|---|---|---|
| [CPython](https://www.python.org/) 3.14 | PSF License | runtime |
| Tcl/Tk (part of CPython) | Tcl/Tk license (BSD-style) | user interface |
| [NumPy](https://numpy.org/) | BSD-3-Clause (bundled OpenBLAS etc.: see its license files) | image processing |
| [Pillow](https://python-pillow.github.io/) | MIT-CMU | screen capture, images |
| [sv-ttk](https://github.com/rdbende/Sun-Valley-ttk-theme) | MIT | Windows 11 theme |
| [PyInstaller](https://pyinstaller.org/) bootloader | GPL-2.0 with bootloader exception | executable |

The full license texts are copied into the `LICENSES` folder of every build.
The installer is built with [Inno Setup](https://jrsoftware.org/isinfo.php) (Inno Setup License).
