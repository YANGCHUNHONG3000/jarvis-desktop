# Third-party software

Jarvis is distributed under the MIT License. Third-party dependencies retain their own licenses and notices; they are not relicensed by the project. No third-party dependency is vendored in this release.

The desktop dependency pins currently documented by the project are:

| Package | Version / condition | Use |
| --- | --- | --- |
| requests | 2.34.2 | HTTP requests |
| beautifulsoup4 | 4.15.0 | HTML parsing |
| playwright | 1.61.0 | Browser automation |
| openpyxl | 3.1.5 | Spreadsheet files |
| pywinauto | 0.6.9 (Windows) | Windows UI automation |
| comtypes | 1.4.17 (Windows) | COM automation |
| pywin32 | 312 (Windows) | Windows integration |
| lxml | 6.1.1 | XML processing |
| tzdata | 2026.2 | Time-zone data fallback |

`pytest==8.4.2` is a development/test dependency, not a runtime dependency. The versions above reflect the project environment, not an independent license audit. Consult each package's own distribution metadata and license before redistribution. Python and Microsoft Edge/WPS Office are separately installed software and are not bundled here.