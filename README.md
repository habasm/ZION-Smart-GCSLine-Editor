# ZION Smart GCS Line Editor
============================
ZION is a password-protected Gradio editor for reviewing and correcting large
line-oriented text datasets stored in Google Cloud Storage (GCS). It loads a
small page of rows at a time, keeps the original source objects unchanged, and
stores edits as sparse patches until an administrator builds a complete output
file.

## Main features

- Edit text directly by logical row number.
- Insert rows above or below an existing row.
- Delete rows without rewriting the source object immediately.
- Paste one or many populated lines without creating extra empty rows.
- Hover over a row to copy its text or paste clipboard content into it.
- Navigate with icon controls above, beside, and below the editor.
- Disable Previous on the first page and Next on the final page.
- Use the editor in normal or fullscreen mode.
- Remember every user's last visited row separately for every file.
- Keep a global personal sticky note for each user.
- Display administrator notes read-only to other users; only the note owner can
  update an administrator note.
- Create users and assign dataset files using the administrator interface.
- Create the next sequential dataset part automatically.
- View built output files in an administrator-only, read-only row viewer.
- Require explicit Save; text changes are not uploaded automatically.

## Dataset layout

The application currently uses these locations:

```text
Source parts: gs://zion_model/dataset/amharic_clean_parts/
Saved patches: gs://zion_model/dataset/editor_patches_v4/
Built files:   gs://zion_model/dataset/editor_built_v4/
```

Dataset part names follow this sequence:

```text
part-000015.txt
part-000016.txt
part-000017.txt
```

Creating a part selects the next available number. The original source parts
are never overwritten by normal editing.

## Requirements

- Linux VPS or Google Compute Engine VM
- Python 3.10 or newer
- A GCS identity that can list, read, create, and update objects in
  `gs://zion_model`
- Nginx for public hosting
- HTTPS for browser clipboard-read permission

Python packages:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "gradio>=5,<6" google-cloud-storage
```

## Google Cloud authentication

On a Google Compute Engine VM, the recommended approach is to attach a service
account with the required bucket permissions to the VM. The Google Cloud
Storage library uses Application Default Credentials automatically.

For development outside Google Compute Engine, configure Application Default
Credentials before starting the editor:

```bash
gcloud auth application-default login
```

If the file list is empty, first verify that the active identity can list
objects under:

```text
gs://zion_model/dataset/amharic_clean_parts/
```

## Configuration

The main settings are near the beginning of `editor.py`:

```python
BUCKET_NAME = "zion_model"
GCS_FOLDER = "dataset/amharic_clean_parts/"
PATCH_FOLDER = "dataset/editor_patches_v4/"
BUILD_FOLDER = "dataset/editor_built_v4/"
PAGE_SIZE = 50
LOCAL_DIR = Path("/home/lookingforitknow/zion-editor/zion_editor_v4")
```

Change `LOCAL_DIR` if the application is installed under a different Linux
account or directory.

### Initial administrator

The first startup creates this account when no administrator exists:

```text
Username: admin
Password: @habasm365
```

Set a different bootstrap password before the first startup:

```bash
export ZION_INITIAL_ADMIN_PASSWORD='a-long-unique-password'
```

The bootstrap environment variable only affects initial account creation. Once
the SQLite database exists, change the password from the Account panel. Change
the default password immediately on a public deployment.

## Run manually

The application directory used by the current VPS is:

```bash
cd /home/lookingforitknow/zion-editor
source .venv/bin/activate
python editor.py
```

The app listens on `127.0.0.1:7860`. It is intentionally not exposed directly
to the internet. Nginx should proxy public requests to this address.

## Run persistently with systemd

Create `/etc/systemd/system/zion-editor.service`:

```ini
[Unit]
Description=ZION Gradio Dataset Editor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=lookingforitknow
Group=lookingforitknow
WorkingDirectory=/home/lookingforitknow/zion-editor
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/lookingforitknow/zion-editor/.venv/bin/python /home/lookingforitknow/zion-editor/editor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The `ExecStart` path must use `.venv/bin/python`. Using `/usr/bin/python3` can
cause missing-package failures and an Nginx `502 Bad Gateway` response.

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable zion-editor
sudo systemctl restart zion-editor
sudo systemctl status zion-editor --no-pager
```

View application logs:

```bash
sudo journalctl -u zion-editor -n 100 --no-pager
```

## HTTPS deployment

The current deployment target is:

```text
https://your-ip
```

Public TCP ports 80 and 443 must be allowed by the Google Cloud firewall. Keep
port 7860 private because Nginx reaches it through localhost.

See [IP_HTTPS_DEPLOYMENT.md](IP_HTTPS_DEPLOYMENT.md) for the Nginx and
Let's Encrypt IP-certificate setup. IP certificates are short-lived, so the
renewal timer must remain enabled.

Useful checks:

```bash
curl -I http://127.0.0.1:7860
curl -I https://your-ip-global
sudo nginx -t
sudo systemctl status nginx --no-pager
sudo systemctl status zion-certbot-renew.timer --no-pager
```

## Editor behavior

### Paste rules

- One populated clipboard line replaces the selected row.
- Two populated lines replace the selected row and insert one row below.
- N populated lines replace the selected row and insert N-1 rows below.
- Blank and whitespace-only clipboard lines are ignored.
- Trailing clipboard newlines do not create dataset rows.

The row Paste icon uses the same rules as keyboard paste. Programmatic clipboard
reading requires HTTPS and browser permission. If permission is blocked, click
the row and use `Ctrl+V`.

### Save and Build

Save and Build are intentionally different operations:

1. **Save** uploads sparse changes as a JSON patch under
   `dataset/editor_patches_v4/`.
2. **Build** materializes the complete edited text under
   `dataset/editor_built_v4/`.

Build updates the same built output name on repeated runs. Build is blocked
while the selected file has unsaved changes.

### Navigation memory

The SQLite database stores a separate last row for every user and source file.
Switching to another file and returning restores the previous file's own row.
The most recently visited file is also restored after login.

### Notes

Notes are global per account and are not associated with the selected dataset
file. Each user can update only their own note. Notes written by administrators
are displayed read-only to other users and administrators.

## Local and cloud storage

Local state is stored under:

```text
/home/lookingforitknow/zion-editor/zion_editor_v4/
```

It includes:

- `editor.db`: users, password hashes, assignments, edit state, navigation
  progress, and notes.
- Line index files used for efficient range loading.
- Temporary files used while producing a complete build.

Large patch JSON is uploaded directly to GCS. Complete built text files are
also stored in GCS. Back up `editor.db` because it contains accounts,
assignments, local edit state, notes, and progress.

## Administrator workflow

1. Sign in as an administrator.
2. Open the Account menu.
3. Create users in Administration.
4. Select a user and check the files they may edit.
5. Save file access.
6. Use Part files to create the next sequential source part.
7. Use Built files viewer to inspect completed outputs without editing them.

Administrators always have full access to all loaded source files.

## Updating the application

Copy the new `editor.py` into:

```text
/home/lookingforitknow/zion-editor/editor.py
```

Then restart the service:

```bash
sudo systemctl restart zion-editor
sudo systemctl status zion-editor --no-pager
```

Perform a hard browser refresh with `Ctrl+Shift+R` after JavaScript or CSS
changes.

## Troubleshooting

### Nginx returns 502

The editor is not listening on port 7860. Check:

```bash
curl -I http://127.0.0.1:7860
sudo journalctl -u zion-editor -n 100 --no-pager
```

Confirm that systemd uses the virtual-environment Python executable.

### Files do not load

- Confirm the VM service account or ADC identity can access the bucket.
- Confirm `BUCKET_NAME` and `GCS_FOLDER` match the real GCS path.
- Restart the service and inspect its logs for a Google Cloud error.

### Row Paste icon fails

- Open the editor through HTTPS.
- Allow clipboard access when prompted by the browser.
- Use `Ctrl+V` as a fallback.

### Interface changes do not appear

Restart the service, then use `Ctrl+Shift+R` in the browser to bypass cached
JavaScript and CSS.

### HTTPS certificate fails

Confirm that the ACME challenge is publicly reachable:

```bash
curl http://your-ip/.well-known/acme-challenge/test
```

Then inspect:

```bash
sudo tail -n 100 /var/log/letsencrypt/letsencrypt.log
```

