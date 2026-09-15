## GUDE Device Manager 0.0.6

- Discover devices without an Internet connection. Download firmware information explicitly with **Fetch Firmware Info** and retain it for offline use.
- Select and upload local firmware without a saved online catalog, including a shared selection for multiple devices of the same model.
- Apply saved device credentials to status queries and updates. Stop rejected firmware uploads before rebooting the device and report the failure.
- Query status immediately after **Add Device**, using the entered connection settings without first saving `upload.ini`. Keep these settings for subsequent updates.
- Fix the theme's startup error caused by unused calendar and upload dependencies.
- Choose the local Web UI port with `--webui-port 8080`; the default remains 8000. Report invalid ports and startup failures clearly.
- Update repository links and add the cached GDM release feed implementation.

### Verification

- 37 automated tests and 8 subtests passed, including browser checks with and without authentication.
- Live status query against an Expert Power Control 8041-1 with Internet requests blocked.
- Windows executable built with PyInstaller; custom-port startup verified on port 8080.

Device HTTP/HTTPS ports remain separate from the local Web UI port. An alternative local port does not resolve every possible Windows socket restriction.
