# Third-party software

Jarvis is distributed under the MIT License. Third-party dependencies retain their own licenses and notices; they are not relicensed by the project. A small MIT-licensed Windows-Use annotation adaptation is included; its complete notice and the Winapp/MCP notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Optional executable runtimes and their dependencies are obtained separately and are not bundled.

The desktop dependency pins currently documented by the project are:

| Package | Version / condition | Use |
| --- | --- | --- |
| Pillow | 11.3.0 (host) | Selected-window annotation |
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
Optional integrations: Microsoft winappCli 0.7.1 (MIT), CursorTouch Windows-MCP at the commit pinned in `requirements-windows-mcp.txt` (MIT), and the isolated OCR dependency set in `requirements-windows-vision.txt`. Each transitive dependency retains its own license. Windows-Use adaptation source and license are recorded in the complete notices. These integrations do not import an upstream agent execution loop.
