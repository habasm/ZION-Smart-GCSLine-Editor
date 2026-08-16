# ============================================================
# ZION SMART GCS LINE EDITOR V4.4.1
# ============================================================
#
# MULTI-LINE PASTE BEHAVIOR
#
# 1 line:
#   Replace current row.
#
# 2 lines:
#   Replace current row with line 1.
#   Insert exactly 1 row below containing line 2.
#
# N lines:
#   Replace current row with line 1.
#   Insert exactly N-1 rows below.
#
# TRAILING CLIPBOARD NEWLINES ARE IGNORED.
#
# ============================================================
#
# SAVE / BUILD BEHAVIOR
#
# SAVE ALL CHANGES:
#   Stores sparse changes as a JSON patch in GCS.
#
# BUILD FINAL TEXT FILE:
#   Creates a complete materialized TXT file.
#
# Original GCS source is NEVER rewritten.
#
# Example:
#
#   Source:
#   gs://zion_model/dataset/file.txt
#
#   Patch:
#   gs://zion_model/dataset/editor_patches_v4/file....json
#
#   Built:
#   gs://zion_model/dataset/editor_built_v4/file.txt
#
# Repeated BUILD operations replace/update the same built file.
#
# ============================================================


# ============================================================
# 1. INSTALL
# ============================================================

# !pip -q install -U google-cloud-storage "gradio>=5,<6"


# ============================================================
# 2. IMPORTS
# ============================================================

import os
import re
import json
import html
import sqlite3
import hashlib
import hmac
import secrets
import traceback
import io
import math
import inspect
import warnings
import struct

from pathlib import Path
from datetime import datetime, timezone

import gradio as gr


from google.cloud import storage


# ============================================================
# 3. CONFIGURATION
# ============================================================

BUCKET_NAME = "zion_model"

GCS_FOLDER = "dataset/amharic_clean_parts/"

PATCH_FOLDER = "dataset/editor_patches_v4/"

# ------------------------------------------------------------
# COMPLETE BUILT TEXT FILES
# ------------------------------------------------------------
#
# BUILD FINAL TEXT FILE will create:
#
# gs://zion_model/dataset/editor_built_v4/<filename>
#
# The same output filename is reused on every BUILD.
#
# ------------------------------------------------------------

BUILD_FOLDER = "dataset/editor_built_v4/"

PAGE_SIZE = 50

CHUNK_SIZE = 16 * 1024 * 1024

LOCAL_DIR = Path(
    "/home/lookingforitknow/zion-editor/zion_editor_v4"
)

LOCAL_DIR.mkdir(
    parents=True,
    exist_ok=True
)

DB_FILE = (
    LOCAL_DIR /
    "editor.db"
)


print("=" * 80)
print("ZION SMART GCS LINE EDITOR V4.4.1")
print("=" * 80)

print(
    f"Local directory: {LOCAL_DIR}"
)

print(
    f"Database:        {DB_FILE}"
)

print(
    f"Build folder:    gs://{BUCKET_NAME}/{BUILD_FOLDER}"
)


# ============================================================
# 4. AUTHENTICATION
# ============================================================

INITIAL_ADMIN_USERNAME = "admin"
INITIAL_ADMIN_PASSWORD = os.environ.get(
    "ZION_INITIAL_ADMIN_PASSWORD",
    "@habasm365"
)


# ============================================================
# 5. GCS
# ============================================================

client = storage.Client()

bucket = client.bucket(
    BUCKET_NAME
)

print(
    f"✓ Connected to gs://{BUCKET_NAME}"
)


# ============================================================
# 6. SQLITE
# ============================================================

db = sqlite3.connect(
    str(DB_FILE),
    check_same_thread=False,
    timeout=30
)

db.execute(
    "PRAGMA journal_mode=WAL"
)

db.execute(
    "PRAGMA synchronous=NORMAL"
)

db.execute(
    "PRAGMA foreign_keys=ON"
)


# ============================================================
# DOCUMENT METADATA
# ============================================================

db.execute(
    """
    CREATE TABLE IF NOT EXISTS document_meta (
        file TEXT PRIMARY KEY,
        generation TEXT NOT NULL,
        size INTEGER NOT NULL,
        original_lines INTEGER NOT NULL,
        current_lines INTEGER NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0,
        updated TEXT NOT NULL
    )
    """
)


# ============================================================
# SPARSE ROW CHANGES
# ============================================================

db.execute(
    """
    CREATE TABLE IF NOT EXISTS rows (
        row_id TEXT PRIMARY KEY,
        file TEXT NOT NULL,
        original_line INTEGER,
        position REAL NOT NULL,
        text TEXT NOT NULL,
        state TEXT NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL,
        updated TEXT NOT NULL
    )
    """
)


# ============================================================
# OPERATION HISTORY
# ============================================================

db.execute(
    """
    CREATE TABLE IF NOT EXISTS operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file TEXT NOT NULL,
        row_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        old_text TEXT,
        new_text TEXT,
        position REAL,
        created TEXT NOT NULL
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created TEXT NOT NULL,
        updated TEXT NOT NULL
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS file_assignments (
        username TEXT NOT NULL COLLATE NOCASE,
        file TEXT NOT NULL,
        created TEXT NOT NULL,
        PRIMARY KEY (username, file),
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    )
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS user_progress (
        username TEXT PRIMARY KEY COLLATE NOCASE,
        file TEXT,
        row_number INTEGER NOT NULL DEFAULT 1,
        updated TEXT NOT NULL,
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    )
    """
)

# Keep an independent cursor for every user/file pair.  ``user_progress`` is
# retained as a small compatibility record for installations created by older
# versions; new navigation reads and writes this per-file table.
db.execute(
    """
    CREATE TABLE IF NOT EXISTS user_file_progress (
        username TEXT NOT NULL COLLATE NOCASE,
        file TEXT NOT NULL,
        row_number INTEGER NOT NULL DEFAULT 1,
        updated TEXT NOT NULL,
        PRIMARY KEY (username, file),
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    )
    """
)

db.execute(
    """
    INSERT OR IGNORE INTO user_file_progress(username, file, row_number, updated)
    SELECT username, file, row_number, updated
    FROM user_progress
    WHERE file IS NOT NULL AND TRIM(file) <> ''
    """
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS user_file_notes (
        username TEXT NOT NULL COLLATE NOCASE,
        file TEXT NOT NULL,
        note_text TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL,
        updated TEXT NOT NULL,
        PRIMARY KEY (username, file),
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    )
    """
)

# Notes are global to a user, not tied to a dataset file.  Keep the earlier
# per-file table for a safe one-time migration, then use this compact table.
db.execute(
    """
    CREATE TABLE IF NOT EXISTS user_notes (
        username TEXT PRIMARY KEY COLLATE NOCASE,
        note_text TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL,
        updated TEXT NOT NULL,
        FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
    )
    """
)

db.execute(
    """
    INSERT OR IGNORE INTO user_notes(username, note_text, created, updated)
    SELECT old.username, old.note_text, old.created, old.updated
    FROM user_file_notes AS old
    WHERE TRIM(old.note_text) <> ''
      AND old.updated = (
          SELECT MAX(candidate.updated)
          FROM user_file_notes AS candidate
          WHERE candidate.username=old.username
      )
    """
)


# ============================================================
# INDEXES
# ============================================================

db.execute(
    """
    CREATE INDEX IF NOT EXISTS
    idx_rows_file_position
    ON rows(file, position)
    """
)

db.execute(
    """
    CREATE INDEX IF NOT EXISTS
    idx_rows_file_original
    ON rows(file, original_line)
    """
)

db.execute(
    """
    CREATE INDEX IF NOT EXISTS
    idx_operations_file
    ON operations(file, id)
    """
)

db.commit()

print(
    f"✓ SQLite ready: {DB_FILE}"
)


# ============================================================
# 7. GLOBAL STATE
# ============================================================

STATE = {
    "file": None,
    "generation": None,
    "size": 0,
    "original_lines": 0,
    "current_lines": 0,
    "start": 1,
    "end": 0,
    "revision": 0,
    "refresh_counter": 0
}


# ============================================================
# 8. HELPERS
# ============================================================

def now_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def hash_password(password, salt=None):
    """Create a salted PBKDF2 password record for local authentication."""

    if salt is None:
        salt = secrets.token_bytes(16)

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password).encode("utf-8"),
        salt,
        310_000
    )
    return "pbkdf2_sha256$310000$" + salt.hex() + "$" + digest.hex()


def verify_password(password, stored):
    try:
        algorithm, iterations, salt_hex, expected_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations)
        )
        return hmac.compare_digest(actual.hex(), expected_hex)
    except (AttributeError, TypeError, ValueError):
        return False


def bootstrap_admin():
    existing = db.execute(
        "SELECT 1 FROM users WHERE username=?",
        (INITIAL_ADMIN_USERNAME,)
    ).fetchone()
    if existing:
        return

    timestamp = now_iso()
    db.execute(
        """
        INSERT INTO users(username, password_hash, is_admin, active, created, updated)
        VALUES (?, ?, 1, 1, ?, ?)
        """,
        (
            INITIAL_ADMIN_USERNAME,
            hash_password(INITIAL_ADMIN_PASSWORD),
            timestamp,
            timestamp
        )
    )
    db.commit()
    print("Created initial admin account: admin")


def authenticate(username, password):
    row = db.execute(
        "SELECT password_hash, active FROM users WHERE username=?",
        (str(username or "").strip(),)
    ).fetchone()
    return bool(row and row[1] and verify_password(password, row[0]))


def request_username(request):
    username = getattr(request, "username", None)
    if not username:
        raise gr.Error("Your login session is unavailable. Please sign in again.")
    return str(username)


def user_is_admin(username):
    row = db.execute(
        "SELECT is_admin, active FROM users WHERE username=?",
        (username,)
    ).fetchone()
    return bool(row and row[0] and row[1])


def accessible_files(username):
    if user_is_admin(username):
        return list(GCS_FILES)

    assigned = db.execute(
        """
        SELECT file FROM file_assignments
        WHERE username=? ORDER BY file
        """,
        (username,)
    ).fetchall()
    allowed = {row[0] for row in assigned}
    return [filename for filename in GCS_FILES if filename in allowed]


def require_file_access(username, filename):
    if not filename or filename not in accessible_files(username):
        raise gr.Error("This file is not assigned to your account.")


def save_user_progress(username, filename, row_number):
    require_file_access(username, filename)
    row_number = max(1, int(row_number or 1))
    db.execute(
        """
        INSERT INTO user_file_progress(username, file, row_number, updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(username, file) DO UPDATE SET
            row_number=excluded.row_number,
            updated=excluded.updated
        """,
        (username, filename, row_number, now_iso())
    )
    db.commit()


def get_user_progress(username, files):
    rows = db.execute(
        """
        SELECT file, row_number
        FROM user_file_progress
        WHERE username=?
        ORDER BY updated DESC
        """,
        (username,)
    ).fetchall()
    allowed = set(files)
    for row in rows:
        if row[0] in allowed:
            return row[0], max(1, int(row[1] or 1))
    return (files[0], 1) if files else (None, 1)


def get_file_progress(username, filename):
    """Return this user's last visible row for one particular file."""

    row = db.execute(
        """
        SELECT row_number FROM user_file_progress
        WHERE username=? AND file=?
        """,
        (username, filename)
    ).fetchone()
    return max(1, int(row[0] or 1)) if row else 1


bootstrap_admin()


def count_words(text):

    if not text:
        return 0

    return len(
        str(text).split()
    )


def safe_key(filename):

    return re.sub(
        r"[^A-Za-z0-9_.-]",
        "_",
        filename
    )


def file_hash(filename):

    return hashlib.sha1(
        filename.encode(
            "utf-8"
        )
    ).hexdigest()[:16]


def index_path(filename):

    return (
        LOCAL_DIR /
        (
            safe_key(filename)
            + "."
            + file_hash(filename)
            + ".idx"
        )
    )


def metadata_path(filename):

    return (
        LOCAL_DIR /
        (
            safe_key(filename)
            + "."
            + file_hash(filename)
            + ".json"
        )
    )


def patch_path(filename):

    return (
        PATCH_FOLDER
        + safe_key(
            Path(filename).name
        )
        + "."
        + file_hash(filename)
        + ".json"
    )


def build_path(filename):

    return (
        BUILD_FOLDER
        + Path(filename).name
    )


def row_id_original(
    filename,
    line
):

    return (
        "orig:"
        + file_hash(filename)
        + ":"
        + str(int(line))
    )


def original_line_from_row_id(filename, row_id):

    """Validate an original-row ID against its owning file."""

    prefix = "orig:" + file_hash(filename) + ":"

    if not isinstance(row_id, str) or not row_id.startswith(prefix):
        raise ValueError("Row does not belong to the selected file.")

    try:
        original_line = int(row_id[len(prefix):])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid original row ID.") from exc

    if original_line < 1:
        raise ValueError("Invalid original row ID.")

    return original_line


def row_id_new():

    raw = (
        f"{datetime.now().timestamp()}:"
        f"{os.urandom(16).hex()}"
    )

    return (
        "new:"
        + hashlib.sha1(
            raw.encode("utf-8")
        ).hexdigest()
    )


def increment_refresh():

    STATE["refresh_counter"] = (
        int(
            STATE.get(
                "refresh_counter",
                0
            )
        )
        + 1
    )

    return STATE["refresh_counter"]


# ============================================================
# 9. GCS FILE DISCOVERY
# ============================================================

def find_gcs_files():

    print()
    print(
        f"Scanning gs://{BUCKET_NAME}/{GCS_FOLDER}"
    )

    files = []

    for blob in client.list_blobs(
        BUCKET_NAME,
        prefix=GCS_FOLDER
    ):

        name = blob.name

        if name.endswith("/"):
            continue

        if name.startswith(PATCH_FOLDER):
            continue

        if name.startswith(BUILD_FOLDER):
            continue

        if name.lower().endswith(
            (
                ".txt",
                ".text",
                ".jsonl",
                ".csv"
            )
        ):

            files.append(name)

    files.sort()

    print(
        f"✓ Found {len(files):,} files"
    )

    for name in files[:10]:

        print(
            "  ",
            name
        )

    if len(files) > 10:

        print(
            f"  ... and {len(files)-10:,} more"
        )

    return files


GCS_FILES = find_gcs_files()


if not GCS_FILES:
    print(
        "No part files are currently loaded from "
        f"gs://{BUCKET_NAME}/{GCS_FOLDER}. "
        "Use the folder refresh or create-part control."
    )


# ============================================================
# 10. BUILD / LOAD LINE INDEX
# ============================================================

def prepare_index(filename):

    blob = bucket.blob(
        filename
    )

    blob.reload()

    generation = str(
        blob.generation
    )

    size = int(
        blob.size or 0
    )

    idx = index_path(
        filename
    )

    meta = metadata_path(
        filename
    )


    if (
        idx.exists()
        and meta.exists()
    ):

        try:

            metadata = json.loads(
                meta.read_text(
                    encoding="utf-8"
                )
            )

            if (
                str(
                    metadata.get(
                        "generation"
                    )
                )
                == generation
                and
                int(
                    metadata.get(
                        "size",
                        -1
                    )
                )
                == size
            ):

                lines = int(
                    metadata["lines"]
                )

                # Older installations stored one decimal offset per line.
                # Convert that generated cache once to fixed-width binary so
                # any row can be reached with one seek instead of rescanning
                # the index from its first line on every page load.
                if metadata.get("index_format") != "uint64-le-v1":
                    binary_tmp = Path(str(idx) + ".binary.tmp")
                    converted = 0

                    with open(idx, "r", encoding="utf-8") as source:
                        with open(binary_tmp, "wb") as target:
                            for value in source:
                                value = value.strip()
                                if not value:
                                    continue
                                target.write(
                                    struct.pack("<Q", int(value))
                                )
                                converted += 1

                    if converted != lines:
                        binary_tmp.unlink(missing_ok=True)
                        raise RuntimeError(
                            "Cached line index count is invalid."
                        )

                    binary_tmp.replace(idx)
                    metadata["index_format"] = "uint64-le-v1"
                    meta.write_text(
                        json.dumps(metadata, indent=2),
                        encoding="utf-8"
                    )

                print(
                    f"✓ Cached index: "
                    f"{lines:,} lines"
                )

                return (
                    idx,
                    lines,
                    size,
                    generation
                )

        except Exception:
            pass


    print()
    print("=" * 80)
    print("BUILDING GCS LINE INDEX")
    print("=" * 80)

    print(
        f"File: {filename}"
    )

    print(
        f"Size: {size/(1024**3):.2f} GB"
    )

    print(
        "Original file is NOT downloaded completely."
    )


    line_count = (
        1
        if size > 0
        else 0
    )

    offset = 0

    tmp_idx = Path(
        str(idx) + ".tmp"
    )


    with open(
        tmp_idx,
        "wb"
    ) as out:

        if size > 0:

            out.write(struct.pack("<Q", 0))


        while offset < size:

            end = min(
                offset + CHUNK_SIZE,
                size
            )

            data = blob.download_as_bytes(
                start=offset,
                end=end - 1
            )


            for match in re.finditer(
                b"\n",
                data
            ):

                next_offset = (
                    offset
                    + match.start()
                    + 1
                )

                if next_offset < size:

                    out.write(
                        struct.pack("<Q", next_offset)
                    )

                    line_count += 1


            offset = end

            percent = (
                offset
                /
                max(size, 1)
            ) * 100

            print(
                f"\rIndexing: {percent:6.2f}%",
                end=""
            )


    print()

    tmp_idx.replace(
        idx
    )


    meta.write_text(
        json.dumps(
            {
                "generation":
                    generation,

                "size":
                    size,

                "lines":
                    line_count,

                "index_format":
                    "uint64-le-v1"
            },
            indent=2
        ),
        encoding="utf-8"
    )


    print(
        f"✓ Indexed {line_count:,} lines"
    )


    return (
        idx,
        line_count,
        size,
        generation
    )


# ============================================================
# 11. INDEX ACCESS
# ============================================================

def offset_for_line(
    idx,
    line_number
):

    if line_number < 1:
        return None


    with open(idx, "rb") as f:
        f.seek((int(line_number) - 1) * 8)
        value = f.read(8)

    if len(value) != 8:
        return None

    return struct.unpack("<Q", value)[0]


def get_range_offsets(
    idx,
    start,
    end
):

    start = max(1, int(start))
    end = int(end)

    if end < start:
        return []

    count = end - start + 1

    with open(idx, "rb") as f:
        f.seek((start - 1) * 8)
        payload = f.read(count * 8)

    if len(payload) % 8:
        raise RuntimeError("Cached line index is truncated.")

    return [
        value[0]
        for value in struct.iter_unpack("<Q", payload)
    ]


# ============================================================
# 12. READ ORIGINAL GCS LINES
# ============================================================

def read_original_lines(
    filename,
    start,
    end
):

    idx = index_path(
        filename
    )

    original_lines = int(
        STATE["original_lines"]
    )


    if original_lines <= 0:
        return []


    start = max(
        1,
        int(start)
    )

    end = min(
        int(end),
        original_lines
    )


    if start > end:
        return []


    offsets = get_range_offsets(
        idx,
        start,
        end
    )


    if not offsets:
        return []


    first_offset = offsets[0]


    if end < original_lines:

        next_offset = offset_for_line(
            idx,
            end + 1
        )

        last_byte = (
            next_offset - 1
        )

    else:

        last_byte = (
            int(STATE["size"]) - 1
        )


    if last_byte < first_offset:
        return []


    raw = bucket.blob(filename).download_as_bytes(
        start=first_offset,
        end=last_byte
    )


    text = raw.decode(
        "utf-8",
        errors="replace"
    )


    lines = text.splitlines()

    result = []


    for i, line_text in enumerate(
        lines[
            : end - start + 1
        ]
    ):

        result.append(
            {
                "original_line":
                    start + i,

                "text":
                    line_text
            }
        )


    return result


# ============================================================
# 13. ENSURE DOCUMENT
# ============================================================

def ensure_document(filename):
    (
        idx,
        original_lines,
        size,
        generation
    ) = prepare_index(
        filename
    )


    meta_row = db.execute(
        """
        SELECT
            generation,
            size,
            original_lines,
            current_lines,
            revision
        FROM document_meta
        WHERE file=?
        """,
        (filename,)
    ).fetchone()


    if meta_row is None:

        db.execute(
            """
            INSERT INTO document_meta(
                file,
                generation,
                size,
                original_lines,
                current_lines,
                revision,
                updated
            )
            VALUES(
                ?, ?, ?, ?, ?, 0, ?
            )
            """,
            (
                filename,
                generation,
                size,
                original_lines,
                original_lines,
                now_iso()
            )
        )

        db.commit()


    else:

        old_generation = str(
            meta_row[0]
        )

        old_size = int(
            meta_row[1]
        )


        if (
            old_generation != generation
            or old_size != size
        ):

            print(
                "⚠ GCS source changed."
            )

            print(
                "Clearing edits for this file."
            )


            db.execute(
                """
                DELETE FROM rows
                WHERE file=?
                """,
                (filename,)
            )


            db.execute(
                """
                DELETE FROM operations
                WHERE file=?
                """,
                (filename,)
            )


            db.execute(
                """
                UPDATE document_meta
                SET
                    generation=?,
                    size=?,
                    original_lines=?,
                    current_lines=?,
                    revision=0,
                    updated=?
                WHERE file=?
                """,
                (
                    generation,
                    size,
                    original_lines,
                    original_lines,
                    now_iso(),
                    filename
                )
            )

            db.commit()


    current = db.execute(
        """
        SELECT
            current_lines,
            revision
        FROM document_meta
        WHERE file=?
        """,
        (filename,)
    ).fetchone()


    # Older releases let repeated inserts after the final source row grow
    # beyond the renderer's source-relative window (N + 1, N + 2, ...).
    # Repair those legacy keys on load so previously hidden rows become
    # visible again.  The helper leaves valid files untouched.
    if repair_inserted_order_keys(
        filename,
        original_lines
    ):

        db.commit()


    STATE["file"] = filename

    STATE["generation"] = generation

    STATE["size"] = size

    STATE["original_lines"] = (
        original_lines
    )

    STATE["current_lines"] = int(
        current[0]
    )

    STATE["revision"] = int(
        current[1]
    )


    return idx


# ============================================================
# 14. ROW RECORD
# ============================================================

def get_row_record(
    filename,
    row_id
):

    return db.execute(
        """
        SELECT
            row_id,
            original_line,
            position,
            text,
            state,
            deleted,
            created,
            updated
        FROM rows
        WHERE file=?
          AND row_id=?
        """,
        (
            filename,
            row_id
        )
    ).fetchone()


# ============================================================
# 15. MODIFIED ORIGINALS
# ============================================================

def get_modified_originals(filename, start=None, end=None):

    sql = """
        SELECT
            row_id,
            original_line,
            text,
            state,
            position,
            deleted
        FROM rows
        WHERE file=?
          AND original_line IS NOT NULL
    """
    parameters = [filename]

    if start is not None:
        sql += " AND original_line>=?"
        parameters.append(int(start))

    if end is not None:
        sql += " AND original_line<=?"
        parameters.append(int(end))

    rows = db.execute(sql, parameters).fetchall()


    result = {}


    for row in rows:

        result[row[0]] = {
            "original_line":
                row[1],

            "text":
                row[2],

            "state":
                row[3],

            "position":
                row[4],

            "deleted":
                bool(row[5])
        }


    return result


# ============================================================
# 16. STRUCTURAL CHANGE CHECK
# ============================================================

def has_structural_changes(filename):

    result = db.execute(
        """
        SELECT 1
        FROM rows
        WHERE file=?
          AND (
              original_line IS NULL
              OR deleted=1
          )
        LIMIT 1
        """,
        (filename,)
    ).fetchone()

    return result is not None


# ============================================================
# 17. FAST PAGE
# ============================================================

def get_fast_page(
    filename,
    start,
    end
):

    original_rows = read_original_lines(
        filename,
        start,
        end
    )


    modified = get_modified_originals(
        filename,
        start,
        end
    )


    result = []


    for row in original_rows:

        original_line = row[
            "original_line"
        ]


        rid = row_id_original(
            filename,
            original_line
        )


        saved = modified.get(
            rid
        )


        if saved:

            if saved["deleted"]:
                continue


            text = saved["text"]

            state = saved["state"]

        else:

            text = row["text"]

            state = "saved"


        result.append(
            {
                "id":
                    rid,

                "line":
                    original_line,

                "words":
                    count_words(text),

                "text":
                    text,

                "status":
                    (
                        "✎"
                        if state == "dirty"
                        else "✓"
                    )
            }
        )


    return result


# ============================================================
# 18. STRUCTURAL PAGE
# ============================================================

def get_structural_page(
    filename,
    start,
    end
):

    original_total = int(
        STATE["original_lines"]
    )


    if original_total <= 0:
        return []


    inserted_before = db.execute(
        """
        SELECT COUNT(*)
        FROM rows
        WHERE file=?
          AND original_line IS NULL
          AND deleted=0
          AND position < ?
        """,
        (
            filename,
            float(start)
        )
    ).fetchone()[0]


    deleted_before = db.execute(
        """
        SELECT COUNT(*)
        FROM rows
        WHERE file=?
          AND original_line IS NOT NULL
          AND deleted=1
          AND original_line < ?
        """,
        (
            filename,
            int(start)
        )
    ).fetchone()[0]


    estimated_original_start = (
        int(start)
        - int(inserted_before)
        + int(deleted_before)
    )


    estimated_original_start = max(
        1,
        estimated_original_start
    )


    margin = PAGE_SIZE + 100


    original_start = max(
        1,
        estimated_original_start - margin
    )

    # Include one source-row boundary before the working range.  New rows use
    # keys between source rows, so this gives the page builder enough context
    # to place a row inserted immediately before the first loaded source row.
    window_start = max(
        1,
        original_start - 1
    )


    original_end = min(
        original_total,
        estimated_original_start
        + PAGE_SIZE
        + margin
    )


    original_rows = read_original_lines(
        filename,
        window_start,
        original_end
    )


    original_map = {
        row["original_line"]:
            row["text"]

        for row in original_rows
    }


    inserted_rows = db.execute(
        """
        SELECT
            row_id,
            position,
            text,
            state
        FROM rows
        WHERE file=?
          AND original_line IS NULL
          AND deleted=0
          AND position >= ?
          AND position <= ?
        ORDER BY
            position ASC,
            row_id ASC
        """,
        (
            filename,
            float(window_start - 1),
            float(original_end + 2)
        )
    ).fetchall()


    events = []


    for (
        rid,
        position,
        text,
        state
    ) in inserted_rows:

        events.append(
            {
                "position":
                    float(position),

                "id":
                    rid,

                "text":
                    text,

                "state":
                    state,

                "kind":
                    "new"
            }
        )


    modified = get_modified_originals(
        filename,
        window_start,
        original_end
    )


    for original_line in range(
        window_start,
        original_end + 1
    ):

        rid = row_id_original(
            filename,
            original_line
        )


        modified_row = modified.get(
            rid
        )


        if modified_row:

            if modified_row["deleted"]:
                continue


            text = modified_row["text"]

            state = modified_row["state"]

            position = float(
                modified_row["position"]
            )

        else:

            text = original_map.get(
                original_line,
                ""
            )

            state = "saved"

            position = float(
                original_line
            )


        events.append(
            {
                "position":
                    position,

                "id":
                    rid,

                "text":
                    text,

                "state":
                    state,

                "kind":
                    "original"
            }
        )


    events.sort(
        key=lambda event: (
            event["position"],
            0
            if event["kind"] == "new"
            else 1,
            event["id"]
        )
    )


    inserted_before_all = db.execute(
        """
        SELECT COUNT(*)
        FROM rows
        WHERE file=?
          AND original_line IS NULL
          AND deleted=0
          AND position < ?
        """,
        (
            filename,
            float(window_start - 1)
        )
    ).fetchone()[0]


    deleted_before_all = db.execute(
        """
        SELECT COUNT(*)
        FROM rows
        WHERE file=?
          AND original_line IS NOT NULL
          AND deleted=1
          AND original_line < ?
        """,
        (
            filename,
            int(window_start)
        )
    ).fetchone()[0]


    before_count = (
        window_start
        - 1
        + int(inserted_before_all)
        - int(deleted_before_all)
    )


    result = []


    for index, event in enumerate(
        events
    ):

        logical_line = (
            before_count
            + index
            + 1
        )


        if logical_line < start:
            continue


        if logical_line > end:
            break


        if event["state"] == "new":

            status = "+"

        elif event["state"] == "dirty":

            status = "✎"

        else:

            status = "✓"


        result.append(
            {
                "id":
                    event["id"],

                "line":
                    logical_line,

                "words":
                    count_words(
                        event["text"]
                    ),

                "text":
                    event["text"],

                "status":
                    status
            }
        )


    return result


# ============================================================
# 19. CURRENT PAGE
# ============================================================

def get_current_page(
    filename,
    start
):
    total = int(
        STATE["current_lines"]
    )


    if total <= 0:
        return []


    start = max(
        1,
        min(
            int(start),
            total
        )
    )


    end = min(
        start + PAGE_SIZE - 1,
        total
    )


    if not has_structural_changes(
        filename
    ):

        return get_fast_page(
            filename,
            start,
            end
        )


    return get_structural_page(
        filename,
        start,
        end
    )


# ============================================================
# 20. PAGE STATUS
# ============================================================

def page_status(filename):

    if not filename:
        return "No file selected."


    total = int(
        STATE["current_lines"]
    )

    start = int(
        STATE["start"]
    )

    end = int(
        STATE["end"]
    )

    size_gb = (
        STATE["size"]
        /
        (1024 ** 3)
    )


    return (
        f"**{html.escape(Path(filename).name)}**  \n"
        f"Rows **{start:,}–{end:,}** / "
        f"**{total:,}** • "
        f"Source **{size_gb:.2f} GB**"
    )


# ============================================================
# 21. EMPTY EDITOR
# ============================================================

def empty_editor_html(message):

    return f"""
    <div class="zion-empty">
        <div class="zion-empty-icon">▤</div>
        <div class="zion-empty-title">ZION Editor</div>
        <div class="zion-empty-text">
            {html.escape(str(message))}
        </div>
    </div>
    """


# ============================================================
# 22. RENDER EDITOR
# ============================================================

def render_editor(rows, filename=None):

    if not rows:

        return empty_editor_html(
            "No rows available."
        )


    body = []


    for row in rows:

        rid = html.escape(
            str(row["id"]),
            quote=True
        )

        line = int(
            row["line"]
        )

        words = int(
            row["words"]
        )

        text = html.escape(
            str(row["text"]),
            quote=False
        )

        status = str(
            row["status"]
        )


        if status == "+":

            status_class = "status-new"

        elif status == "✎":

            status_class = "status-dirty"

        else:

            status_class = "status-saved"


        body.append(
            f"""
            <div
                class="zion-row"
                data-row-id="{rid}"
                data-line="{line}"
            >

                <div class="zion-line">
                    {line:,}
                </div>

                <div
                    class="zion-words"
                    data-words
                >
                    {words:,}
                </div>

                <div class="zion-text">

                    <textarea
                        class="zion-editor"
                        data-editor
                        spellcheck="false"
                        autocomplete="off"
                        autocorrect="off"
                        autocapitalize="off"
                    >{text}</textarea>

                </div>

                <div
                    class="zion-status {status_class}"
                    data-status
                >
                    {status}
                </div>

                <div class="zion-actions">

                    <button
                        type="button"
                        class="zion-clipboard-action"
                        data-copy-row
                        data-zion-tooltip="Copy this row"
                        title="Copy this row"
                        aria-label="Copy this row"
                    >&#x2398;</button>

                    <button
                        type="button"
                        class="zion-clipboard-action"
                        data-paste-row
                        data-zion-tooltip="Paste into this row"
                        title="Paste into this row"
                        aria-label="Paste into this row"
                    >&#x1F4CB;</button>

                    <button
                        type="button"
                        data-add-above
                        title="Insert row above"
                    >
                        ↑
                    </button>

                    <button
                        type="button"
                        data-add-below
                        title="Insert row below"
                    >
                        ↓
                    </button>

                    <button
                        type="button"
                        data-delete
                        title="Delete row"
                    >
                        ×
                    </button>

                </div>

            </div>
            """
        )


    unsaved = False
    if filename:
        unsaved = bool(db.execute(
            """
            SELECT 1 FROM rows
            WHERE file=? AND state IN ('dirty', 'new', 'deleted')
            LIMIT 1
            """,
            (filename,)
        ).fetchone())

    return f"""
    <div
        id="zion-editor"
        data-unsaved="{1 if unsaved else 0}"
        data-start="{int(STATE.get('start') or 1)}"
        data-end="{int(STATE.get('end') or 0)}"
        data-total="{int(STATE.get('current_lines') or 0)}"
    >

        <button
            type="button"
            class="zion-mid-nav zion-mid-previous"
            data-zion-nav="previous"
            aria-label="Previous page"
            title="Previous page"
        >&#8249;</button>

        <button
            type="button"
            class="zion-mid-nav zion-mid-next"
            data-zion-nav="next"
            aria-label="Next page"
            title="Next page"
        >&#8250;</button>

        <button
            type="button"
            class="zion-inline-fullscreen"
            data-zion-fullscreen
            aria-label="Toggle full screen"
            title="Toggle full screen"
        >&#x26F6;</button>

        <div class="zion-header">
            <div>LINE</div>
            <div>WORDS</div>
            <div>TEXT</div>
            <div>STATUS</div>
            <div>ACTIONS</div>
        </div>

        <div class="zion-body">
            {''.join(body)}
        </div>

        <div class="zion-footer">
            <span>✓ Saved</span>
            <span>✎ Unsaved</span>
            <span>+ New</span>
            <span>Text only editable</span>
            <span>Paste multiple lines</span>
        </div>

    </div>
    """


# ============================================================
# 23. LOAD PAGE
# ============================================================

def load_page(
    filename,
    start
):

    if not filename:

        return (
            empty_editor_html(
                "Select a GCS file."
            ),
            "No file selected.",
            1,
            0
        )


    try:

        ensure_document(
            filename
        )


        total = int(
            STATE["current_lines"]
        )


        if total <= 0:

            start = 1
            end = 0

        else:

            start = max(
                1,
                min(
                    int(start or 1),
                    total
                )
            )

            end = min(
                start + PAGE_SIZE - 1,
                total
            )


        STATE["start"] = start

        STATE["end"] = end


        rows = get_current_page(
            filename,
            start
        )


        return (
            render_editor(rows, filename),
            page_status(filename),
            start,
            total
        )


    except Exception as e:

        traceback.print_exc()


        return (
            empty_editor_html(
                f"ERROR: {e}"
            ),
            f"❌ **ERROR:** `{html.escape(str(e))}`",
            1,
            0
        )


# ============================================================
# 24. FILE SELECTION
# ============================================================

def select_file(filename, request: gr.Request):
    username = request_username(request)
    require_file_access(username, filename)
    start = get_file_progress(username, filename)
    STATE["refresh_counter"] = 0
    result = load_page(filename, start)
    save_user_progress(username, filename, result[2])
    return result


def _admin_notes_html(viewer_username):
    """Render global administrator notes without exposing edit controls."""

    rows = db.execute(
        """
        SELECT n.username, n.note_text, n.updated
        FROM user_notes AS n
        JOIN users AS u ON u.username=n.username
        WHERE u.is_admin=1 AND u.active=1
          AND n.username<>? AND TRIM(n.note_text)<>''
        ORDER BY n.updated DESC
        """,
        (viewer_username,)
    ).fetchall()
    if not rows:
        return ""

    cards = []
    for owner, note_text, updated in rows:
        cards.append(
            '<div class="zion-shared-note">'
            f'<div class="zion-shared-note-owner">Admin: {html.escape(owner)}</div>'
            f'<div class="zion-shared-note-text">{html.escape(note_text)}</div>'
            '</div>'
        )
    return (
        '<div class="zion-shared-notes-title">Administrator notes</div>'
        + ''.join(cards)
    )


def load_global_notes(request: gr.Request):
    """Load the signed-in user's global editable sticky note."""

    username = request_username(request)
    row = db.execute(
        """
        SELECT note_text FROM user_notes
        WHERE username=?
        """,
        (username,)
    ).fetchone()
    return (
        row[0] if row else "",
        _admin_notes_html(username),
        "Your personal global notepad",
        ""
    )


def save_global_note(note_text, request: gr.Request):
    """Save only the current user's note; no user can overwrite its owner."""

    username = request_username(request)
    note_text = str(note_text or "").strip()
    if len(note_text) > 20_000:
        raise gr.Error("A note can contain at most 20,000 characters.")

    if note_text:
        timestamp = now_iso()
        db.execute(
            """
            INSERT INTO user_notes(
                username, note_text, created, updated
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                note_text=excluded.note_text,
                updated=excluded.updated
            """,
            (username, note_text, timestamp, timestamp)
        )
        message = "Note saved."
    else:
        db.execute(
            "DELETE FROM user_notes WHERE username=?",
            (username,)
        )
        message = "Note cleared."
    db.commit()
    return note_text, _admin_notes_html(username), message


def open_global_notes_panel(request: gr.Request):
    note, admin_notes, context, message = load_global_notes(request)
    return gr.Group(visible=True), note, admin_notes, context, message


def close_global_notes_panel():
    return gr.Group(visible=False)


def load_user_workspace(request: gr.Request):
    username = request_username(request)
    files = accessible_files(username)
    filename, start = get_user_progress(username, files)
    admin = user_is_admin(username)
    managed_users = admin_usernames() if admin else []
    managed_user = username if admin else None
    built_files = built_file_names() if admin else []

    if not filename:
        page = (
            empty_editor_html("No file has been assigned to your account."),
            "Ask an administrator to assign a file.",
            1,
            0
        )
    else:
        page = load_page(filename, start)
        save_user_progress(username, filename, page[2])

    return (
        gr.Dropdown(choices=files, value=filename),
        page[0], page[1], page[2], page[3],
        f"Signed in as **{html.escape(username)}**"
        + (" · Administrator" if admin else ""),
        gr.Group(visible=admin),
        gr.Dropdown(choices=managed_users, value=managed_user),
        gr.CheckboxGroup(
            choices=GCS_FILES,
            value=assigned_files(managed_user) if admin else []
        ),
        (
            "Administrator accounts always have full access to every file."
            if admin else ""
        ),
        assignment_summary() if admin else "",
        gr.Dropdown(
            choices=built_files,
            value=(built_files[-1] if built_files else None)
        )
    )


# ============================================================
# 25. NEXT PAGE
# ============================================================

def next_page(filename, start, request: gr.Request):

    username = request_username(request)
    require_file_access(username, filename)

    if not filename:

        return load_page(filename, 1)


    total = int(
        STATE["current_lines"]
    )


    new_start = (
        int(start or 1)
        + PAGE_SIZE
    )


    if new_start > total:

        new_start = int(start or 1)


    result = load_page(
        filename,
        new_start
    )
    save_user_progress(username, filename, result[2])
    return result


# ============================================================
# 26. PREVIOUS PAGE
# ============================================================

def previous_page(filename, start, request: gr.Request):

    username = request_username(request)
    require_file_access(username, filename)

    if not filename:

        return load_page(filename, 1)


    total = int(
        STATE["current_lines"]
    )


    new_start = (
        int(start or 1)
        - PAGE_SIZE
    )


    if new_start < 1:

        new_start = 1


    result = load_page(
        filename,
        new_start
    )
    save_user_progress(username, filename, result[2])
    return result


# ============================================================
# 27. REFRESH
# ============================================================

def refresh_current(
    filename,
    start,
    request: gr.Request
):

    username = request_username(request)
    require_file_access(username, filename)

    if not filename:

        return (
            empty_editor_html(
                "Select a GCS file."
            ),
            "No file selected.",
            1,
            0
        )


    result = load_page(
        filename,
        int(start or 1)
    )
    save_user_progress(username, filename, result[2])
    return result


# ============================================================
# 28. UPDATE DOCUMENT COUNTS
# ============================================================

def update_document_counts(filename):

    meta = db.execute(
        """
        SELECT original_lines
        FROM document_meta
        WHERE file=?
        """,
        (filename,)
    ).fetchone()


    if not meta:
        return


    original_lines = int(
        meta[0]
    )


    inserted = db.execute(
        """
        SELECT COUNT(*)
        FROM rows
        WHERE file=?
          AND original_line IS NULL
          AND deleted=0
        """,
        (filename,)
    ).fetchone()[0]


    deleted_original = db.execute(
        """
        SELECT COUNT(*)
        FROM rows
        WHERE file=?
          AND original_line IS NOT NULL
          AND deleted=1
        """,
        (filename,)
    ).fetchone()[0]


    current_lines = (
        original_lines
        + int(inserted)
        - int(deleted_original)
    )


    db.execute(
        """
        UPDATE document_meta
        SET
            current_lines=?,
            updated=?
        WHERE file=?
        """,
        (
            current_lines,
            now_iso(),
            filename
        )
    )


    STATE["current_lines"] = (
        current_lines
    )


# ============================================================
# 29. MARK ORIGINAL DIRTY
# ============================================================

def mark_original_dirty(
    filename,
    row_id,
    new_text
):

    existing = get_row_record(
        filename,
        row_id
    )


    timestamp = now_iso()


    if existing:

        old_text = existing[3]


        if old_text == new_text:
            return False


        db.execute(
            """
            UPDATE rows
            SET
                text=?,
                state='dirty',
                deleted=0,
                updated=?
            WHERE file=?
              AND row_id=?
            """,
            (
                new_text,
                timestamp,
                filename,
                row_id
            )
        )


        position = float(
            existing[2]
        )


    else:

        try:
            original_line = original_line_from_row_id(
                filename,
                row_id
            )
        except ValueError:
            return False

        original = read_original_lines(
            filename,
            original_line,
            original_line
        )


        if not original:
            return False


        old_text = original[0]["text"]


        if old_text == new_text:
            return False


        position = float(
            original_line
        )


        db.execute(
            """
            INSERT INTO rows(
                row_id,
                file,
                original_line,
                position,
                text,
                state,
                deleted,
                created,
                updated
            )
            VALUES(
                ?, ?, ?, ?, ?,
                'dirty', 0, ?, ?
            )
            """,
            (
                row_id,
                filename,
                original_line,
                position,
                new_text,
                timestamp,
                timestamp
            )
        )


    db.execute(
        """
        INSERT INTO operations(
            file,
            row_id,
            operation,
            old_text,
            new_text,
            position,
            created
        )
        VALUES(
            ?, ?, 'edit', ?, ?, ?, ?
        )
        """,
        (
            filename,
            row_id,
            old_text,
            new_text,
            position,
            timestamp
        )
    )


    db.commit()

    return True


# ============================================================
# 30. APPLY SINGLE EDIT
# ============================================================

def apply_text_edit(
    filename,
    row_id,
    text
):

    if not filename:

        return {
            "ok": False,
            "message":
                "No file selected."
        }


    text = (
        ""
        if text is None
        else str(text)
    )


    row = get_row_record(
        filename,
        row_id
    )


    # A delayed browser edit must never resurrect a row after a delete has
    # already completed.
    if row and bool(row[5]):

        return {
            "ok": False,
            "message": "Row was deleted. Refresh before editing it again."
        }


    if row is None:

        try:
            original_line_from_row_id(filename, row_id)
        except ValueError as e:
            return {
                "ok": False,
                "message": str(e)
            }


    if row and row[1] is None:

        old_text = row[3]


        if old_text == text:

            return {
                "ok": True,
                "message":
                    "✓ No change."
            }


        db.execute(
            """
            UPDATE rows
            SET
                text=?,
                state='new',
                deleted=0,
                updated=?
            WHERE file=?
              AND row_id=?
            """,
            (
                text,
                now_iso(),
                filename,
                row_id
            )
        )


        db.execute(
            """
            INSERT INTO operations(
                file,
                row_id,
                operation,
                old_text,
                new_text,
                position,
                created
            )
            VALUES(
                ?, ?, 'edit', ?, ?, ?, ?
            )
            """,
            (
                filename,
                row_id,
                old_text,
                text,
                row[2],
                now_iso()
            )
        )


        db.commit()


        return {
            "ok": True,
            "message":
                "✎ New row edited."
        }


    changed = mark_original_dirty(
        filename,
        row_id,
        text
    )


    return {
        "ok": True,
        "message":
            (
                "✎ Line edited."
                if changed
                else
                "✓ No change."
            )
    }


# ============================================================
# 31. NORMALIZE MULTILINE PASTE
# ============================================================
def normalize_pasted_lines(text):

    # --------------------------------------------------------
    # IMPORTANT:
    # JavaScript may already send a list of logical lines.
    # Do NOT convert that list to str().
    # --------------------------------------------------------

    if isinstance(text, list):

        lines = []

        for value in text:

            lines.append(
                ""
                if value is None
                else str(value)
            )

        # Blank clipboard entries are separators/noise, not dataset rows.
        # Only populated logical lines should be inserted.
        return [line for line in lines if line.strip()]


    # --------------------------------------------------------
    # Normal string input
    # --------------------------------------------------------

    if text is None:
        return [""]


    normalized = str(text)

    # Normalize newline formats
    normalized = normalized.replace(
        "\r\n",
        "\n"
    )

    normalized = normalized.replace(
        "\r",
        "\n"
    )

    normalized = normalized.replace(
        "\u2028",
        "\n"
    )

    normalized = normalized.replace(
        "\u2029",
        "\n"
    )

    normalized = normalized.replace(
        "\u000b",
        "\n"
    )

    normalized = normalized.replace(
        "\u000c",
        "\n"
    )


    raw_lines = normalized.split(
        "\n"
    )


    # Do not create dataset rows for empty or whitespace-only clipboard lines.
    return [line for line in raw_lines if line.strip()]

# ============================================================
# 31B. STABLE INSERTION ORDER KEYS
# ============================================================

def repair_inserted_order_keys(
    filename,
    original_total,
    force=False
):

    """Keep active inserted rows inside stable source-row intervals.

    Early versions extended the ordering key by one every time a user
    inserted below the last added row.  The page loader intentionally reads
    only a small source-relative window, so sufficiently large keys made
    otherwise valid rows disappear.  Rebalancing preserves the existing
    visible order while placing every inserted row strictly between source
    anchors (or between the last source row and the N+1 sentinel).

    No commit is performed here.  Callers may be in the middle of a larger
    paste/insert transaction and must be able to roll the whole action back.
    """

    original_total = int(original_total or 0)

    if original_total <= 0:
        return False

    upper_sentinel = float(original_total + 1)

    if not force:
        invalid = db.execute(
            """
            SELECT 1
            FROM rows
            WHERE file=?
              AND original_line IS NULL
              AND deleted=0
              AND (
                  position<=0
                  OR position>=?
                  OR position=CAST(position AS INTEGER)
              )
            LIMIT 1
            """,
            (filename, upper_sentinel)
        ).fetchone()

        if invalid is None:
            return False

    rows = db.execute(
        """
        SELECT row_id, position
        FROM rows
        WHERE file=?
          AND original_line IS NULL
          AND deleted=0
        ORDER BY position ASC, row_id ASC
        """,
        (filename,)
    ).fetchall()

    if not rows:
        return False

    groups = {}

    for row_id, raw_position in rows:
        position = float(raw_position)

        if not math.isfinite(position):
            next_anchor = original_total + 1
        else:
            next_anchor = int(math.ceil(position))
            next_anchor = max(
                1,
                min(original_total + 1, next_anchor)
            )

        groups.setdefault(next_anchor, []).append(row_id)

    for next_anchor, row_ids in groups.items():
        lower = float(next_anchor - 1)
        upper = float(next_anchor)
        step = (upper - lower) / (len(row_ids) + 1)

        for index, row_id in enumerate(row_ids, start=1):
            db.execute(
                """
                UPDATE rows
                SET
                    position=?,
                    state=CASE
                        WHEN state='saved' THEN 'new'
                        ELSE state
                    END,
                    updated=?
                WHERE file=? AND row_id=?
                """,
                (
                    lower + step * index,
                    now_iso(),
                    filename,
                    row_id
                )
            )

    return True


def find_insertion_order_key(
    filename,
    target_row_id,
    direction,
    count=1,
    _allow_repair=True
):

    """Return stable internal ordering keys for rows inserted at a target.

    ``position`` is an ordering key, not a display row number.  Source rows
    keep their integer keys (1, 2, 3, ...); inserted rows use a key between
    their visible neighbours.  The UI calculates and displays the resulting
    logical row numbers, so users always see consecutive spreadsheet rows.

    This is deliberately sparse: inserting below row 2 never rewrites every
    source row after row 2, which is essential for large files.
    """

    if direction not in {"above", "below"}:
        raise ValueError("Insertion direction must be 'above' or 'below'.")

    if count < 1:
        return []

    target = get_row_record(filename, target_row_id)

    if target is None:
        try:
            original_line = original_line_from_row_id(
                filename,
                target_row_id
            )
        except ValueError as exc:
            raise ValueError("Target row not found.") from exc

        target_key = float(original_line)
        target_is_new = False
    else:
        if bool(target[5]):
            raise ValueError("Target row has been deleted.")

        target_key = float(target[2])
        target_is_new = target[1] is None

    total_row = db.execute(
        """
        SELECT original_lines
        FROM document_meta
        WHERE file=?
        """,
        (filename,)
    ).fetchone()
    original_total = int(total_row[0]) if total_row else 0

    def original_is_visible(original_line):
        record = db.execute(
            """
            SELECT deleted
            FROM rows
            WHERE file=?
              AND original_line=?
            """,
            (filename, original_line)
        ).fetchone()
        return record is None or not bool(record[0])

    def previous_original_key(limit):
        line = min(original_total, int(math.ceil(limit)) - 1)
        while line >= 1:
            if original_is_visible(line):
                return float(line)
            line -= 1
        return None

    def next_original_key(limit, include_equal=False):
        line = (
            int(math.ceil(limit))
            if include_equal
            else int(math.floor(limit)) + 1
        )
        while line <= original_total:
            if original_is_visible(line):
                return float(line)
            line += 1
        return None

    if direction == "above":
        lower_row = db.execute(
            """
            SELECT MAX(position)
            FROM rows
            WHERE file=?
              AND deleted=0
              AND original_line IS NULL
              AND position < ?
            """,
            (filename, target_key)
        ).fetchone()
        lower_candidates = [
            float(lower_row[0]) if lower_row[0] is not None else None,
            previous_original_key(target_key),
        ]
        lower = max(key for key in lower_candidates if key is not None) \
            if any(key is not None for key in lower_candidates) else None
        upper = target_key
    else:
        upper_row = db.execute(
            """
            SELECT MIN(position)
            FROM rows
            WHERE file=?
              AND deleted=0
              AND original_line IS NULL
              AND position > ?
            """,
            (filename, target_key)
        ).fetchone()
        upper_candidates = [
            float(upper_row[0]) if upper_row[0] is not None else None,
            next_original_key(
                target_key,
                include_equal=(
                    target_is_new
                    and target_key.is_integer()
                )
            ),
        ]
        upper = min(key for key in upper_candidates if key is not None) \
            if any(key is not None for key in upper_candidates) else None
        lower = target_key

    # Keep both edge intervals bounded as (0, 1) and (N, N+1).  Older code
    # used ``upper = lower + 1`` at the end, which let repeated inserts drift
    # beyond the page builder's source-relative range and disappear.
    if lower is None:
        lower = 0.0
    if upper is None:
        upper = float(original_total + 1)

    gap = upper - lower
    keys = [
        lower + (gap * index / (count + 1))
        for index in range(1, count + 1)
    ]

    # Floating-point gaps can eventually become too dense after many
    # repeated inserts at exactly the same boundary.  Spread the existing
    # rows within their source intervals and retry once.
    if (
        gap <= 0
        or any(
            not math.isfinite(key)
            or key <= lower
            or key >= upper
            for key in keys
        )
        or len(set(keys)) != len(keys)
    ):
        if _allow_repair and repair_inserted_order_keys(
            filename,
            original_total,
            force=True
        ):
            return find_insertion_order_key(
                filename,
                target_row_id,
                direction,
                count,
                _allow_repair=False
            )

        raise RuntimeError(
            "Could not create a stable row position. Refresh and try again."
        )

    return keys
# ============================================================
# 32. MULTILINE REPLACEMENT
# ============================================================

def replace_with_multiple_lines(
    filename,
    row_id,
    text
):
    """
    Spreadsheet-style multiline paste.

    Example:

        Current:
            2  OLD ROW 2
            3  OLD ROW 3
            4  OLD ROW 4
            5  OLD ROW 5

        Paste:
            AAA
            BBB
            CCC

        Result:
            2  AAA
            3  BBB
            4  CCC
            5  OLD ROW 3
            6  OLD ROW 4
            7  OLD ROW 5

    Rules:

    - First pasted line replaces the selected row.
    - Remaining pasted lines are inserted directly below it.
    - Existing rows below are shifted downward in the displayed row numbers.
    - Original GCS line numbers are never changed.
    - The current page remains in place after the operation.
    """

    if not filename:
        return {
            "ok": False,
            "message": "No file selected."
        }

    # ========================================================
    # NORMALIZE PASTED CONTENT
    # ========================================================

    lines = normalize_pasted_lines(
        text
    )

    if not lines:
        lines = [""]

    pasted_count = len(lines)

    # ========================================================
    # SINGLE LINE
    # ========================================================
    #
    # A single line behaves exactly like normal editing.
    #
    # ========================================================

    if pasted_count == 1:

        return apply_text_edit(
            filename,
            row_id,
            lines[0]
        )

    # ========================================================
    # GET TARGET ROW
    # ========================================================

    row = get_row_record(
        filename,
        row_id
    )

    timestamp = now_iso()

    if row and bool(row[5]):

        return {
            "ok": False,
            "message": "Target row was deleted. Refresh and paste again."
        }

    # ========================================================
    # TARGET ROW DOES NOT YET EXIST IN SPARSE DB
    # ========================================================

    if row is None:

        try:
            original_line = original_line_from_row_id(
                filename,
                row_id
            )
        except ValueError as e:

            return {
                "ok": False,
                "message": str(e)
            }

        # ----------------------------------------------------
        # READ ORIGINAL ROW
        # ----------------------------------------------------

        original = read_original_lines(
            filename,
            original_line,
            original_line
        )

        if not original:

            return {
                "ok": False,
                "message": "Original row not found."
            }

        old_text = original[0]["text"]

        target_position = float(
            original_line
        )

        # ----------------------------------------------------
        # CREATE SPARSE RECORD FOR ORIGINAL ROW
        # ----------------------------------------------------

        db.execute(
            """
            INSERT INTO rows(
                row_id,
                file,
                original_line,
                position,
                text,
                state,
                deleted,
                created,
                updated
            )
            VALUES(
                ?, ?, ?, ?, ?,
                'dirty', 0, ?, ?
            )
            """,
            (
                row_id,
                filename,
                original_line,
                target_position,
                lines[0],
                timestamp,
                timestamp
            )
        )

    # ========================================================
    # TARGET ROW ALREADY EXISTS IN SPARSE DB
    # ========================================================

    else:

        old_text = row[3]

        target_position = float(
            row[2]
        )

        # ----------------------------------------------------
        # UPDATE TARGET ROW WITH FIRST PASTED LINE
        # ----------------------------------------------------

        db.execute(
            """
            UPDATE rows
            SET
                text=?,
                state=?,
                deleted=0,
                updated=?
            WHERE file=?
              AND row_id=?
            """,
            (
                lines[0],

                (
                    "new"
                    if row[1] is None
                    else "dirty"
                ),

                timestamp,
                filename,
                row_id
            )
        )

    # ========================================================
    # RECORD EDIT OPERATION
    # ========================================================

    db.execute(
        """
        INSERT INTO operations(
            file,
            row_id,
            operation,
            old_text,
            new_text,
            position,
            created
        )
        VALUES(
            ?, ?, 'edit', ?, ?, ?, ?
        )
        """,
        (
            filename,
            row_id,
            old_text,
            lines[0],
            target_position,
            timestamp
        )
    )

    # ========================================================
    # NUMBER OF NEW ROWS
    # ========================================================
    #
    # First pasted line replaced the target.
    #
    # Everything after that must become a NEW row.
    #
    # Example:
    #
    # 4 pasted lines
    #
    # line 1 -> replace target
    # line 2 -> new row
    # line 3 -> new row
    # line 4 -> new row
    #
    # Therefore:
    #
    # additional_count = 4 - 1 = 3
    #
    # ========================================================

    additional_count = (
        pasted_count - 1
    )

    # ========================================================
    # INSERT ADDITIONAL PASTED LINES
    # ========================================================

    insertion_keys = find_insertion_order_key(
        filename,
        row_id,
        "below",
        additional_count
    )

    created_count = 0

    for index, line_text in enumerate(
        lines[1:],
        start=1
    ):

        # ``position`` is a stable internal order key.  The renderer turns
        # those keys into the consecutive logical row numbers shown to users.
        new_position = insertion_keys[index - 1]

        new_id = row_id_new()

        # ----------------------------------------------------
        # CREATE NEW ROW
        # ----------------------------------------------------

        db.execute(
            """
            INSERT INTO rows(
                row_id,
                file,
                original_line,
                position,
                text,
                state,
                deleted,
                created,
                updated
            )
            VALUES(
                ?, ?, NULL, ?, ?,
                'new', 0, ?, ?
            )
            """,
            (
                new_id,
                filename,
                float(new_position),
                line_text,
                timestamp,
                timestamp
            )
        )

        # ----------------------------------------------------
        # RECORD INSERT OPERATION
        # ----------------------------------------------------

        db.execute(
            """
            INSERT INTO operations(
                file,
                row_id,
                operation,
                old_text,
                new_text,
                position,
                created
            )
            VALUES(
                ?, ?, 'insert',
                '', ?, ?, ?
            )
            """,
            (
                filename,
                new_id,
                line_text,
                float(new_position),
                timestamp
            )
        )

        created_count += 1

    # ========================================================
    # UPDATE DOCUMENT COUNTS
    # ========================================================

    update_document_counts(
        filename
    )

    # ========================================================
    # COMMIT
    # ========================================================

    db.commit()

    # ========================================================
    # RESULT
    # ========================================================

    return {
        "ok": True,
        "message": (
            f"✎ Replaced 1 row and added "
            f"{created_count:,} row"
            +
            (
                ""
                if created_count == 1
                else "s"
            )
            +
            " below."
        )
    }
    
# ============================================================
# 33. INSERT ROW
# ============================================================

def insert_row(
    filename,
    row_id,
    text,
    direction
):

    if not filename:

        return {
            "ok": False,
            "message":
                "No file selected."
        }


    text = (
        ""
        if text is None
        else str(text)
    )

    if not text.strip():
        return {
            "ok": False,
            "message": "Enter text before inserting a row. Empty rows are not allowed."
        }


    try:

        position = find_insertion_order_key(
            filename,
            row_id,
            direction,
            1
        )[0]

    except Exception as e:

        return {
            "ok": False,
            "message":
                str(e)
        }


    new_id = row_id_new()

    timestamp = now_iso()


    db.execute(
        """
        INSERT INTO rows(
            row_id,
            file,
            original_line,
            position,
            text,
            state,
            deleted,
            created,
            updated
        )
        VALUES(
            ?, ?, NULL, ?, ?,
            'new', 0, ?, ?
        )
        """,
        (
            new_id,
            filename,
            position,
            text,
            timestamp,
            timestamp
        )
    )


    db.execute(
        """
        INSERT INTO operations(
            file,
            row_id,
            operation,
            old_text,
            new_text,
            position,
            created
        )
        VALUES(
            ?, ?, 'insert',
            '', ?, ?, ?
        )
        """,
        (
            filename,
            new_id,
            text,
            position,
            timestamp
        )
    )


    update_document_counts(
        filename
    )


    db.commit()


    return {
        "ok": True,
        "message":
            f"＋ Row inserted {direction}."
    }


# ============================================================
# 35. DELETE
# ============================================================

def delete_row(
    filename,
    row_id
):

    if not filename:

        return {
            "ok": False,
            "message":
                "No file selected."
        }


    timestamp = now_iso()


    row = get_row_record(
        filename,
        row_id
    )


    if row:

        if bool(row[5]):

            return {
                "ok": False,
                "message": "Row is already deleted."
            }

        old_text = row[3]

        position = row[2]


        db.execute(
            """
            UPDATE rows
            SET
                deleted=1,
                state='deleted',
                updated=?
            WHERE file=?
              AND row_id=?
            """,
            (
                timestamp,
                filename,
                row_id
            )
        )


    else:

        try:
            original_line = original_line_from_row_id(
                filename,
                row_id
            )
        except ValueError as e:
            return {
                "ok": False,
                "message": str(e)
            }


        original = read_original_lines(
            filename,
            original_line,
            original_line
        )


        if not original:

            return {
                "ok": False,
                "message":
                    "Original row not found."
            }


        old_text = original[0]["text"]

        position = float(
            original_line
        )


        db.execute(
            """
            INSERT INTO rows(
                row_id,
                file,
                original_line,
                position,
                text,
                state,
                deleted,
                created,
                updated
            )
            VALUES(
                ?, ?, ?, ?, ?,
                'deleted', 1, ?, ?
            )
            """,
            (
                row_id,
                filename,
                original_line,
                position,
                old_text,
                timestamp,
                timestamp
            )
        )


    db.execute(
        """
        INSERT INTO operations(
            file,
            row_id,
            operation,
            old_text,
            new_text,
            position,
            created
        )
        VALUES(
            ?, ?, 'delete',
            ?, '', ?, ?
        )
        """,
        (
            filename,
            row_id,
            old_text,
            position,
            timestamp
        )
    )


    update_document_counts(
        filename
    )


    db.commit()


    return {
        "ok": True,
        "message":
            "🗑 Row deleted."
    }


# ============================================================
# 36. EVENT PROCESSOR
# ============================================================

def process_editor_event(
    payload,
    filename,
    current_start,
    request: gr.Request
):

    username = request_username(request)
    require_file_access(username, filename)
    target_start = max(1, int(current_start or 1))
    event_ack = ""

    def response(message, structural=False):
        """Return the event result and, for row changes, the refreshed page.

        Structural actions used to rely on a hidden counter's secondary
        ``change`` event.  That browser/backend round trip is not reliable in
        all Gradio 5 releases, so return the refreshed editor directly.
        """

        if structural:
            editor_html, page_message, start, total = load_page(
                filename,
                target_start
            )
            save_user_progress(username, filename, start)
            return (
                event_ack, message, STATE["refresh_counter"],
                editor_html, page_message, start, total
            )

        return (
            event_ack, message, STATE["refresh_counter"],
            gr.skip(), gr.skip(), gr.skip(), STATE["current_lines"]
        )

    if not payload:
        return response("")


    try:

        event = json.loads(
            payload
        )

    except Exception as e:

        return response(f"❌ Invalid event: {e}")


    event_type = event.get(
        "type"
    )

    event_ack = str(event.get("event_id") or "")

    try:
        selected_line = max(1, int(event.get("line") or target_start))
    except (TypeError, ValueError):
        selected_line = target_start

    row_id = event.get(
        "row_id"
    )


    try:

        if event_type == "position":

            save_user_progress(username, filename, selected_line)
            return response("")

        # Insert/delete/multiline paste must keep the current page anchored at
        # its existing start.  Using the clicked row as the new page start
        # made every structural action look like the surrounding rows had
        # jumped or been renumbered incorrectly.
        save_user_progress(username, filename, target_start)

        if event_type == "edit":

            result = apply_text_edit(
                filename,
                row_id,
                event.get(
                    "text",
                    ""
                )
            )


            return response(result["message"])


        if event_type == "multiline_confirmed":

          # --------------------------------------------------------
          # Receive explicit logical lines from JavaScript.
          # --------------------------------------------------------

          event_lines = event.get("lines")


          if isinstance(event_lines, list):

              # Send the already-separated lines directly.
              result = replace_with_multiple_lines(
                  filename,
                  row_id,
                  event_lines
              )


          else:

              # Backward compatibility with older events.

              multiline_text = event.get(
                  "text",
                  ""
              )


              result = replace_with_multiple_lines(
                  filename,
                  row_id,
                  multiline_text
              )


          if result.get("ok"):

              increment_refresh()


          return response(result["message"], result.get("ok", False))


        if event_type == "insert":

            result = insert_row(
                filename,
                row_id,
                event.get(
                    "text",
                    ""
                ),
                event.get(
                    "direction",
                    "below"
                )
            )


            if result.get("ok"):

                increment_refresh()


            return response(result["message"], result.get("ok", False))


        if event_type == "delete":

            result = delete_row(
                filename,
                row_id
            )


            if result.get("ok"):

                increment_refresh()


            return response(result["message"], result.get("ok", False))


        return response("Unknown editor event.")


    except Exception as e:

        traceback.print_exc()


        db.rollback()
        return response(
            f"❌ EVENT FAILED: `{html.escape(str(e))}`"
        )


# ============================================================
# 37. BUILD PATCH DATA
# ============================================================

def build_patch_data(
    filename,
    revision
):

    rows = db.execute(
        """
        SELECT
            row_id,
            original_line,
            position,
            text,
            state,
            deleted,
            created,
            updated
        FROM rows
        WHERE file=?
        ORDER BY
            position ASC,
            row_id ASC
        """,
        (filename,)
    ).fetchall()


    records = []


    for row in rows:

        records.append(
            {
                "row_id":
                    row[0],

                "original_line":
                    row[1],

                "position":
                    row[2],

                "text":
                    row[3],

                "state":
                    row[4],

                "deleted":
                    bool(row[5]),

                "created":
                    row[6],

                "updated":
                    row[7]
            }
        )


    metadata = db.execute(
        """
        SELECT
            generation,
            size,
            original_lines,
            current_lines
        FROM document_meta
        WHERE file=?
        """,
        (filename,)
    ).fetchone()


    if not metadata:

        raise RuntimeError(
            "Document metadata not found."
        )


    return {
        "schema":
            "zion-smart-editor-v4.2",

        "file":
            filename,

        "bucket":
            BUCKET_NAME,

        "revision":
            revision,

        "updated":
            now_iso(),

        "generation":
            metadata[0],

        "size":
            metadata[1],

        "original_lines":
            metadata[2],

        "current_lines":
            metadata[3],

        "rows":
            records
    }


# ============================================================
# 38. UPLOAD PATCH
# ============================================================

def sync_patch(
    filename,
    revision
):

    content = build_patch_data(
        filename,
        revision
    )


    payload = json.dumps(
        content,
        ensure_ascii=False,
        indent=2
    )


    path = patch_path(
        filename
    )


    blob = bucket.blob(
        path
    )


    blob.upload_from_string(
        payload,
        content_type="application/json"
    )


    print()
    print(
        "✓ Patch uploaded:"
    )

    print(
        f"gs://{BUCKET_NAME}/{path}"
    )


    return path


# ============================================================
# 39. SAVE ALL
# ============================================================

def save_all_changes(filename, request: gr.Request):

    username = request_username(request)
    require_file_access(username, filename)

    if not filename:

        return (
            "❌ No file selected."
        )


    try:

        ensure_document(
            filename
        )


        dirty = db.execute(
            """
            SELECT COUNT(*)
            FROM rows
            WHERE file=?
              AND state IN(
                  'dirty',
                  'new'
              )
              AND deleted=0
            """,
            (filename,)
        ).fetchone()[0]


        deleted = db.execute(
            """
            SELECT COUNT(*)
            FROM rows
            WHERE file=?
              AND deleted=1
              AND state='deleted'
            """,
            (filename,)
        ).fetchone()[0]


        if (
            dirty == 0
            and deleted == 0
        ):

            return (
                "✓ Nothing to save."
            )


        current_revision = db.execute(
            """
            SELECT revision
            FROM document_meta
            WHERE file=?
            """,
            (filename,)
        ).fetchone()[0]


        revision = (
            int(current_revision)
            + 1
        )


        patch_gcs_path = sync_patch(
            filename,
            revision
        )


        timestamp = now_iso()


        db.execute(
            """
            UPDATE rows
            SET
                state='saved',
                updated=?
            WHERE file=?
              AND state IN(
                  'dirty',
                  'new'
              )
              AND deleted=0
            """,
            (
                timestamp,
                filename
            )
        )


        db.execute(
            """
            UPDATE document_meta
            SET
                revision=?,
                updated=?
            WHERE file=?
            """,
            (
                revision,
                timestamp,
                filename
            )
        )


        db.commit()


        STATE["revision"] = revision


        return (
            f"✓ Saved revision "
            f"**{revision:,}** • "
            f"{dirty:,} changed/new • "
            f"{deleted:,} deleted  \n"
            f"`gs://{BUCKET_NAME}/{patch_gcs_path}`"
        )


    except Exception as e:

        traceback.print_exc()


        return (
            f"❌ SAVE FAILED: "
            f"`{html.escape(str(e))}`"
        )


# ============================================================
# 40. BUILD FINAL TEXT FILE
# ============================================================
#
# Creates the COMPLETE edited text file.
#
# SOURCE:
#
#   gs://zion_model/dataset/filename.txt
#
# OUTPUT:
#
#   gs://zion_model/dataset/editor_built_v4/filename.txt
#
# The original source file is NEVER modified.
#
# Every BUILD updates the SAME output object.
#
# The source is streamed line-by-line.
#
# SQLite contains only sparse changes.
#
# ============================================================

def build_final_text_file(
    filename,
    request: gr.Request,
    progress=gr.Progress()
):

    username = request_username(request)
    require_file_access(username, filename)

    if not filename:

        return (
            "❌ No file selected."
        )

    pending = db.execute(
        """
        SELECT 1 FROM rows
        WHERE file=? AND state IN ('dirty', 'new', 'deleted')
        LIMIT 1
        """,
        (filename,)
    ).fetchone()
    if pending:
        return "❌ Save the pending changes before building the final file."


    temp_output = None


    try:

        progress(
            0,
            desc="Preparing final file..."
        )

        print()
        print("=" * 80)
        print("BUILDING FINAL TEXT FILE")
        print("=" * 80)


        # ----------------------------------------------------
        # ENSURE DOCUMENT STATE
        # ----------------------------------------------------

        ensure_document(
            filename
        )

        expected_source_lines = max(
            1,
            int(STATE["original_lines"])
        )


        # ----------------------------------------------------
        # FINAL OUTPUT PATH
        # ----------------------------------------------------

        output_filename = build_path(
            filename
        )


        print(
            f"Source:"
        )

        print(
            f"gs://{BUCKET_NAME}/{filename}"
        )

        print()

        print(
            f"Output:"
        )

        print(
            f"gs://{BUCKET_NAME}/{output_filename}"
        )


        # ----------------------------------------------------
        # READ ALL SPARSE EDITS
        # ----------------------------------------------------

        sparse_rows = db.execute(
            """
            SELECT
                original_line,
                position,
                text,
                state,
                deleted,
                row_id
            FROM rows
            WHERE file=?
            ORDER BY
                position ASC,
                row_id ASC
            """,
            (filename,)
        ).fetchall()

        empty_inserted = db.execute(
            """
            SELECT COUNT(*)
            FROM rows
            WHERE file=?
              AND original_line IS NULL
              AND deleted=0
              AND TRIM(text)=''
            """,
            (filename,)
        ).fetchone()[0]

        if int(empty_inserted) > 0:
            raise RuntimeError(
                "Build blocked: "
                f"{int(empty_inserted):,} empty inserted row(s) exist. "
                "Edit or delete them before building."
            )


        # ----------------------------------------------------
        # ORIGINAL LINE CHANGES
        # ----------------------------------------------------

        original_changes = {}


        # ----------------------------------------------------
        # INSERTED ROWS
        # ----------------------------------------------------

        inserted_rows = []


        for row in sparse_rows:

            (
                original_line,
                position,
                text,
                state,
                deleted,
                row_id
            ) = row


            # -----------------------------------------------
            # NEW ROW
            # -----------------------------------------------

            if original_line is None:

                if not deleted:

                    inserted_rows.append(
                        {
                            "position":
                                float(position),

                            "text":
                                str(text),

                            "row_id":
                                str(row_id)
                        }
                    )


            # -----------------------------------------------
            # ORIGINAL ROW
            # -----------------------------------------------

            else:

                original_changes[
                    int(original_line)
                ] = {
                    "position":
                        float(position),

                    "text":
                        str(text),

                    "deleted":
                        bool(deleted),

                    "row_id":
                        str(row_id)
                }


        # ----------------------------------------------------
        # SORT INSERTED ROWS
        # ----------------------------------------------------

        inserted_rows.sort(
            key=lambda item: (
                item["position"],
                item["row_id"]
            )
        )


        print()
        print(
            f"Sparse records: "
            f"{len(sparse_rows):,}"
        )

        print(
            f"Inserted rows: "
            f"{len(inserted_rows):,}"
        )

        print(
            f"Original changes: "
            f"{len(original_changes):,}"
        )


        # ----------------------------------------------------
        # SOURCE BLOB
        # ----------------------------------------------------

        source_blob = bucket.blob(
            filename
        )

        source_blob.reload()


        source_size = int(
            source_blob.size or 0
        )


        print(
            f"Source size: "
            f"{source_size / (1024 ** 3):.2f} GB"
        )


        # ----------------------------------------------------
        # LOCAL TEMPORARY OUTPUT
        # ----------------------------------------------------
        #
        # Important:
        #
        # We create a temporary local file first.
        #
        # The existing final GCS file is not touched until
        # the complete build succeeds.
        #
        # ----------------------------------------------------

        temp_output = (
            LOCAL_DIR
            /
            (
                safe_key(
                    Path(filename).name
                )
                + ".final_build.tmp"
            )
        )


        if temp_output.exists():

            temp_output.unlink()


        # ----------------------------------------------------
        # BUILD COUNTERS
        # ----------------------------------------------------

        source_lines = 0

        output_lines = 0

        modified_count = 0

        deleted_count = 0

        inserted_written = 0


        inserted_index = 0


        # ----------------------------------------------------
        # STREAM SOURCE FILE
        # ----------------------------------------------------

        print()
        print(
            "Reading source and applying edits..."
        )

        progress(
            0.03,
            desc="Applying saved edits..."
        )


        with source_blob.open(
            "rb"
        ) as source_stream:


            text_stream = io.TextIOWrapper(
                source_stream,
                encoding="utf-8",
                errors="replace",
                newline=""
            )


            with open(
                temp_output,
                "w",
                encoding="utf-8",
                newline=""
            ) as out:


                for raw_line in text_stream:

                    source_lines += 1


                    original_line_number = (
                        source_lines
                    )


                    current_position = float(
                        original_line_number
                    )


                    # -----------------------------------------
                    # INSERTED ROWS BEFORE CURRENT ORIGINAL ROW
                    # -----------------------------------------

                    while (
                        inserted_index
                        <
                        len(inserted_rows)
                        and
                        inserted_rows[
                            inserted_index
                        ]["position"]
                        <
                        current_position
                    ):

                        inserted = inserted_rows[
                            inserted_index
                        ]


                        out.write(
                            inserted["text"]
                        )

                        out.write(
                            "\n"
                        )


                        output_lines += 1

                        inserted_written += 1

                        inserted_index += 1


                    # -----------------------------------------
                    # CHECK FOR ORIGINAL CHANGE
                    # -----------------------------------------

                    change = (
                        original_changes.get(
                            original_line_number
                        )
                    )


                    if change is not None:

                        # -------------------------------------
                        # DELETE
                        # -------------------------------------

                        if change["deleted"]:

                            deleted_count += 1

                            continue


                        # -------------------------------------
                        # MODIFIED
                        # -------------------------------------

                        out.write(
                            change["text"]
                        )

                        out.write(
                            "\n"
                        )


                        output_lines += 1

                        modified_count += 1


                    else:

                        # -------------------------------------
                        # ORIGINAL UNCHANGED
                        # -------------------------------------

                        clean_line = raw_line.rstrip(
                            "\r\n"
                        )


                        out.write(
                            clean_line
                        )

                        out.write(
                            "\n"
                        )


                        output_lines += 1


                    # -----------------------------------------
                    # PROGRESS
                    # -----------------------------------------

                    if (
                        source_lines % 100000 == 0
                    ):

                        progress(
                            min(
                                0.90,
                                0.03
                                + 0.87
                                * source_lines
                                / expected_source_lines
                            ),
                            desc=(
                                "Building final file: "
                                f"{source_lines:,} / "
                                f"{expected_source_lines:,} rows"
                            )
                        )

                        print(
                            f"\r"
                            f"Source lines: "
                            f"{source_lines:,} | "
                            f"Output lines: "
                            f"{output_lines:,}",
                            end=""
                        )


                # ------------------------------------------------
                # WRITE INSERTED ROWS AFTER LAST ORIGINAL
                # ------------------------------------------------

                while (
                    inserted_index
                    <
                    len(inserted_rows)
                ):

                    inserted = inserted_rows[
                        inserted_index
                    ]


                    out.write(
                        inserted["text"]
                    )

                    out.write(
                        "\n"
                    )


                    output_lines += 1

                    inserted_written += 1

                    inserted_index += 1


        print()

        progress(
            0.92,
            desc="Verifying final row count..."
        )


        # ----------------------------------------------------
        # VERIFY EXPECTED FINAL LINE COUNT
        # ----------------------------------------------------

        metadata = db.execute(
            """
            SELECT
                original_lines,
                current_lines
            FROM document_meta
            WHERE file=?
            """,
            (filename,)
        ).fetchone()


        if metadata:

            expected_original = int(
                metadata[0]
            )

            expected_current = int(
                metadata[1]
            )

            if (
                source_lines
                !=
                expected_original
            ):

                raise RuntimeError(
                    "Source line count changed "
                    "during build. "
                    f"Expected {expected_original:,}, "
                    f"read {source_lines:,}."
                )


            if (
                output_lines
                !=
                expected_current
            ):

                raise RuntimeError(
                    "Final line count mismatch. "
                    f"Expected {expected_current:,}, "
                    f"built {output_lines:,}."
                )


        # ----------------------------------------------------
        # LOCAL FILE SIZE
        # ----------------------------------------------------

        local_size = int(
            temp_output.stat().st_size
        )


        print()
        print(
            "✓ Local final file created."
        )

        print(
            f"Lines: "
            f"{output_lines:,}"
        )

        print(
            f"Size: "
            f"{local_size / (1024 ** 3):.2f} GB"
        )


        # ----------------------------------------------------
        # UPLOAD
        # ----------------------------------------------------
        #
        # SAME GCS OBJECT NAME.
        #
        # Therefore:
        #
        # BUILD 1:
        #   editor_built_v4/file.txt
        #
        # BUILD 2:
        #   editor_built_v4/file.txt
        #
        # BUILD 3:
        #   editor_built_v4/file.txt
        #
        # No _1, _2, _3 copies.
        #
        # ----------------------------------------------------

        print()
        print(
            "Uploading final text file to GCS..."
        )

        progress(
            0.95,
            desc="Uploading final file..."
        )


        output_blob = bucket.blob(
            output_filename
        )


        output_blob.upload_from_filename(
            str(temp_output),
            content_type="text/plain"
        )


        # ----------------------------------------------------
        # VERIFY UPLOAD
        # ----------------------------------------------------

        output_blob.reload()


        uploaded_size = int(
            output_blob.size or 0
        )


        if uploaded_size != local_size:

            raise RuntimeError(
                "Uploaded file size verification failed. "
                f"Local={local_size:,}, "
                f"GCS={uploaded_size:,}."
            )

        progress(
            1,
            desc="Build complete."
        )


        # ----------------------------------------------------
        # CLEAN TEMP FILE
        # ----------------------------------------------------

        try:

            temp_output.unlink()

        except Exception:

            pass


        # ----------------------------------------------------
        # SUCCESS
        # ----------------------------------------------------

        print()
        print("=" * 80)
        print("✓ FINAL TEXT FILE BUILT SUCCESSFULLY")
        print("=" * 80)

        print(
            f"Source:"
        )

        print(
            f"gs://{BUCKET_NAME}/{filename}"
        )

        print()

        print(
            f"Final:"
        )

        print(
            f"gs://{BUCKET_NAME}/{output_filename}"
        )

        print()

        print(
            f"Source lines: "
            f"{source_lines:,}"
        )

        print(
            f"Final lines: "
            f"{output_lines:,}"
        )

        print(
            f"Modified: "
            f"{modified_count:,}"
        )

        print(
            f"Inserted: "
            f"{inserted_written:,}"
        )

        print(
            f"Deleted: "
            f"{deleted_count:,}"
        )

        print(
            f"Final size: "
            f"{uploaded_size / (1024 ** 3):.2f} GB"
        )

        print("=" * 80)


        return (
            f"## ✓ FINAL TEXT FILE BUILT\n\n"
            f"**Source:**  \n"
            f"`gs://{BUCKET_NAME}/{filename}`\n\n"
            f"**Final file:**  \n"
            f"`gs://{BUCKET_NAME}/{output_filename}`\n\n"
            f"**Final lines:** "
            f"**{output_lines:,}**  \n"
            f"**Final size:** "
            f"**{uploaded_size / (1024 ** 3):.2f} GB**  \n"
            f"**Modified:** "
            f"**{modified_count:,}**  \n"
            f"**Inserted:** "
            f"**{inserted_written:,}**  \n"
            f"**Deleted:** "
            f"**{deleted_count:,}**\n\n"
            f"Original GCS file was **not changed**."
        )


    except Exception as e:

        traceback.print_exc()


        if temp_output is not None:

            try:

                if temp_output.exists():

                    temp_output.unlink()

            except Exception:

                pass


        return (
            f"❌ **BUILD FAILED:** "
            f"`{html.escape(str(e))}`"
        )


# ============================================================
# 41. ACCOUNT MANAGEMENT
# ============================================================

def change_own_password(current_password, new_password, confirm_password,
                        request: gr.Request):
    username = request_username(request)
    row = db.execute(
        "SELECT password_hash FROM users WHERE username=? AND active=1",
        (username,)
    ).fetchone()
    if not row or not verify_password(current_password, row[0]):
        return "❌ Current password is incorrect."
    if len(str(new_password or "")) < 8:
        return "❌ The new password must contain at least 8 characters."
    if new_password != confirm_password:
        return "❌ New passwords do not match."

    db.execute(
        "UPDATE users SET password_hash=?, updated=? WHERE username=?",
        (hash_password(new_password), now_iso(), username)
    )
    db.commit()
    return "✅ Password changed. Use the new password at your next login."


def admin_usernames():
    return [
        row[0] for row in db.execute(
            "SELECT username FROM users WHERE active=1 ORDER BY username"
        ).fetchall()
    ]


def assigned_files(username):
    if not username:
        return []
    if user_is_admin(username):
        return list(GCS_FILES)
    return [
        row[0] for row in db.execute(
            "SELECT file FROM file_assignments WHERE username=? ORDER BY file",
            (username,)
        ).fetchall()
        if row[0] in GCS_FILES
    ]


def load_user_file_access(username, request: gr.Request):
    require_admin(request)
    if user_is_admin(username):
        return (
            gr.CheckboxGroup(
                choices=GCS_FILES,
                value=GCS_FILES,
                label="Full administrator file access"
            ),
            "✅ Administrator accounts always have full access to every file."
        )
    return (
        gr.CheckboxGroup(
            choices=GCS_FILES,
            value=assigned_files(username),
            label="Files this user can access"
        ),
        f"Select access for **{html.escape(str(username or 'no user selected'))}**."
    )


def save_user_file_access(username, selected_files, request: gr.Request):
    require_admin(request)
    if not db.execute(
        "SELECT 1 FROM users WHERE username=? AND active=1",
        (username,)
    ).fetchone():
        return "❌ Select a valid user.", assignment_summary()

    if user_is_admin(username):
        return (
            "✅ Administrator access is automatic and includes every file.",
            assignment_summary()
        )

    selected = {name for name in (selected_files or []) if name in GCS_FILES}
    db.execute("DELETE FROM file_assignments WHERE username=?", (username,))
    timestamp = now_iso()
    db.executemany(
        """
        INSERT INTO file_assignments(username, file, created)
        VALUES (?, ?, ?)
        """,
        [(username, filename, timestamp) for filename in sorted(selected)]
    )
    db.commit()
    return (
        f"✅ Saved {len(selected):,} file permission(s) for **{html.escape(username)}**.",
        assignment_summary()
    )


def assignment_summary():
    rows = db.execute(
        """
        SELECT u.username, a.file, u.is_admin
        FROM users u
        LEFT JOIN file_assignments a ON a.username=u.username
        WHERE u.active=1
        ORDER BY u.username, a.file
        """
    ).fetchall()
    grouped = {}
    admin_flags = {}
    for username, filename, is_admin in rows:
        grouped.setdefault(username, [])
        admin_flags[username] = bool(is_admin)
        if filename:
            grouped[username].append(filename)
    if not grouped:
        return "No editor users have been created."
    return "\n".join(
        f"- **{html.escape(username)}:** "
        + (
            f"Full administrator access ({len(GCS_FILES):,} files)"
            if admin_flags.get(username)
            else (", ".join(html.escape(name) for name in files) if files else "No files assigned")
        )
        for username, files in grouped.items()
    )


def require_admin(request):
    username = request_username(request)
    if not user_is_admin(username):
        raise gr.Error("Administrator access is required.")
    return username


def create_user(username, password, confirm_password, make_admin,
                request: gr.Request):
    require_admin(request)
    username = str(username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
        return (
            "❌ Username must be 3–64 letters, numbers, dots, dashes, or underscores.",
            gr.skip(),
            gr.skip(),
            assignment_summary()
        )
    if len(str(password or "")) < 8:
        return "❌ Password must contain at least 8 characters.", gr.skip(), gr.skip(), assignment_summary()
    if password != confirm_password:
        return "❌ Temporary passwords do not match.", gr.skip(), gr.skip(), assignment_summary()
    if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        return "❌ That username already exists.", gr.skip(), gr.skip(), assignment_summary()

    timestamp = now_iso()
    db.execute(
        """
        INSERT INTO users(username, password_hash, is_admin, active, created, updated)
        VALUES (?, ?, ?, 1, ?, ?)
        """,
        (username, hash_password(password), int(bool(make_admin)), timestamp, timestamp)
    )
    db.commit()
    return (
        f"✅ User **{html.escape(username)}** created.",
        gr.Dropdown(choices=admin_usernames(), value=username),
        gr.CheckboxGroup(choices=GCS_FILES, value=[]),
        assignment_summary()
    )


def set_file_assignment(username, filename, grant, request: gr.Request):
    require_admin(request)
    if not db.execute(
        "SELECT 1 FROM users WHERE username=? AND active=1",
        (username,)
    ).fetchone():
        return "❌ Select a valid user.", assignment_summary()
    if filename not in GCS_FILES:
        return "❌ Select a valid file.", assignment_summary()

    if grant:
        db.execute(
            """
            INSERT OR IGNORE INTO file_assignments(username, file, created)
            VALUES (?, ?, ?)
            """,
            (username, filename, now_iso())
        )

        db.execute(
            """
            UPDATE rows
            SET state='saved_deleted', updated=?
            WHERE file=? AND deleted=1 AND state='deleted'
            """,
            (timestamp, filename)
        )
        message = "✅ File assigned."
    else:
        db.execute(
            "DELETE FROM file_assignments WHERE username=? AND file=?",
            (username, filename)
        )
        message = "✅ File assignment removed."
    db.commit()
    return message, assignment_summary()


def grant_file_assignment(username, filename, request: gr.Request):
    return set_file_assignment(username, filename, True, request)


def revoke_file_assignment(username, filename, request: gr.Request):
    return set_file_assignment(username, filename, False, request)


def refresh_gcs_file_cache():
    """Reload the configured part folder without restarting the app."""
    discovered = find_gcs_files()
    GCS_FILES[:] = discovered
    return GCS_FILES


def folder_browser_text(files=None):
    names = list(GCS_FILES if files is None else files)
    if not names:
        return f"No files found in `gs://{BUCKET_NAME}/{GCS_FOLDER}`."
    recent = names[-50:]
    lines = "\n".join(f"- `{html.escape(name)}`" for name in recent)
    prefix = f"**{len(names):,} part file(s)** in `gs://{BUCKET_NAME}/{GCS_FOLDER}`"
    if len(names) > len(recent):
        prefix += f"  \nShowing the latest {len(recent)} files."
    return prefix + "\n\n" + lines


def browse_dataset_folder(current_file, selected_user, request: gr.Request):
    username = request_username(request)
    refresh_gcs_file_cache()
    allowed = accessible_files(username)
    selected = current_file if current_file in allowed else (allowed[0] if allowed else None)
    return (
        gr.Dropdown(choices=allowed, value=selected),
        gr.CheckboxGroup(
            choices=GCS_FILES,
            value=assigned_files(selected_user) if user_is_admin(username) else []
        ),
        folder_browser_text(allowed),
        f"✅ Refreshed `gs://{BUCKET_NAME}/{GCS_FOLDER}`."
    )


def next_part_object_name():
    pattern = re.compile(
        r"^" + re.escape(GCS_FOLDER) + r"part-(\d{6})\.txt$"
    )
    numbers = []
    for filename in GCS_FILES:
        match = pattern.fullmatch(filename)
        if match:
            numbers.append(int(match.group(1)))
    return GCS_FOLDER + f"part-{(max(numbers, default=-1) + 1):06d}.txt"


def upload_dataset_file(uploaded_path, current_file, selected_user,
                        request: gr.Request):
    username = request_username(request)
    if not uploaded_path:
        return "❌ Choose a text file to upload.", gr.skip(), gr.skip(), gr.skip()

    source = Path(str(uploaded_path))
    if source.suffix.lower() not in {".txt", ".text"}:
        return "❌ Choose a TXT or TEXT file.", gr.skip(), gr.skip(), gr.skip()

    try:
        refresh_gcs_file_cache()
        object_name = next_part_object_name()
        # Generation precondition prevents overwriting if another uploader
        # creates the same next part between folder refresh and upload.
        bucket.blob(object_name).upload_from_filename(
            str(source), if_generation_match=0
        )
        GCS_FILES.append(object_name)
        GCS_FILES.sort()

        if not user_is_admin(username):
            db.execute(
                """
                INSERT OR IGNORE INTO file_assignments(username, file, created)
                VALUES (?, ?, ?)
                """,
                (username, object_name, now_iso())
            )
            db.commit()

        return (
            f"✅ Created `{object_name}`. The name was assigned automatically.",
            gr.Dropdown(choices=accessible_files(username), value=object_name),
            gr.CheckboxGroup(
                choices=GCS_FILES,
                value=assigned_files(selected_user) if user_is_admin(username) else []
            ),
            folder_browser_text(accessible_files(username))
        )
    except Exception as exc:
        traceback.print_exc()
        return (
            f"❌ Upload failed: `{html.escape(str(exc))}`",
            gr.skip(), gr.skip(), gr.skip()
        )


def built_file_names():
    return sorted(
        blob.name
        for blob in client.list_blobs(BUCKET_NAME, prefix=BUILD_FOLDER)
        if blob.name != BUILD_FOLDER and not blob.name.endswith("/")
    )


def refresh_built_file_viewer(request: gr.Request):
    require_admin(request)
    files = built_file_names()
    selected = files[-1] if files else None
    return (
        gr.Dropdown(choices=files, value=selected),
        f"✅ Found {len(files):,} built file(s) in `gs://{BUCKET_NAME}/{BUILD_FOLDER}`."
    )


def render_built_rows(rows):
    if not rows:
        return empty_editor_html("This built file has no rows.")

    body = []
    for row in rows:
        line = int(row["line"])
        text = html.escape(str(row["text"]), quote=False)
        words = count_words(row["text"])
        body.append(
            f"""
            <div class="zion-row zion-built-row">
                <div class="zion-line">{line:,}</div>
                <div class="zion-words">{words:,}</div>
                <div class="zion-text">
                    <textarea class="zion-editor zion-built-text"
                        readonly aria-readonly="true">{text}</textarea>
                </div>
                <div class="zion-status status-saved" title="Built row">✓</div>
            </div>
            """
        )

    return f"""
    <div id="zion-built-viewer" class="zion-readonly-viewer">
        <div class="zion-header zion-built-row">
            <div>LINE</div><div>WORDS</div><div>TEXT</div><div>STATUS</div>
        </div>
        <div class="zion-body">{''.join(body)}</div>
        <div class="zion-footer">
            <span>👁 Read-only built file</span>
            <span>Editing and row actions are disabled</span>
        </div>
    </div>
    """


def load_built_file_page(filename, start, request: gr.Request):
    require_admin(request)
    if not filename or not str(filename).startswith(BUILD_FOLDER):
        return (
            empty_editor_html("Select a built file."),
            "Select a valid built file.", 1, 0
        )

    blob = bucket.blob(filename)
    if not blob.exists(client):
        return (
            empty_editor_html("Built file not found."),
            "❌ The selected built file no longer exists.", 1, 0
        )

    idx, total, size, _generation = prepare_index(filename)
    if total <= 0:
        return render_built_rows([]), f"**{html.escape(filename)}** · Empty file", 1, 0

    start = max(1, min(int(start or 1), total))
    end = min(start + PAGE_SIZE - 1, total)
    offsets = get_range_offsets(idx, start, end)
    if not offsets:
        return render_built_rows([]), "No rows available.", start, total

    first_offset = offsets[0]
    if end < total:
        next_offset = offset_for_line(idx, end + 1)
        last_byte = next_offset - 1
    else:
        last_byte = int(size) - 1

    raw = blob.download_as_bytes(start=first_offset, end=last_byte)
    logical_lines = raw.decode("utf-8", errors="replace").splitlines()
    rows = [
        {"line": start + index, "text": text}
        for index, text in enumerate(logical_lines[:end - start + 1])
    ]
    status = (
        f"**{html.escape(filename)}**  \n"
        f"Rows **{start:,}–{end:,}** / **{total:,}** · Read only"
    )
    return render_built_rows(rows), status, start, total


def next_built_file_page(filename, start, request: gr.Request):
    return load_built_file_page(filename, int(start or 1) + PAGE_SIZE, request)


def previous_built_file_page(filename, start, request: gr.Request):
    return load_built_file_page(filename, max(1, int(start or 1) - PAGE_SIZE), request)


# ============================================================
# 42. CSS
# ============================================================

CSS = r"""
.gradio-container {
    max-width: none !important;
    width: 100% !important;
    margin: 0 !important;
    padding-left: 12px !important;
    padding-right: 12px !important;
}

.zion-toolbar {
    position: sticky;
    top: 0;
    z-index: 20;
    align-items: end !important;
    gap: 8px !important;
    margin: 0 0 8px !important;
    padding: 10px 12px !important;
    border: 1px solid var(--block-border-color, #dfe6ee);
    border-radius: 12px;
    background: var(--block-background-fill, rgba(255,255,255,.96));
    box-shadow: 0 4px 18px rgba(15,23,42,.06);
    backdrop-filter: blur(10px);
}

.zion-toolbar button {
    min-width: 38px !important;
    min-height: 38px !important;
    padding: 0 10px !important;
    border-radius: 8px !important;
    font-weight: 650 !important;
}

#zion_go_button button,
#zion_previous_button button,
#zion_next_button button,
#zion_refresh_button button,
#zion_fullscreen_button button,
#zion_save_button button,
#zion_build_button button,
#zion_help_button button,
#zion_account_button button {
    width: 38px !important;
    padding: 0 !important;
    font-size: 17px !important;
}

#zion_help_button button {
    min-width: 38px !important;
    width: 38px !important;
    padding: 0 !important;
    border-radius: 50% !important;
    font-size: 17px !important;
    font-weight: 800 !important;
}

#zion_save_status {
    min-height: 0;
    margin: 0 0 8px;
    font-size: 13px;
}

#zion_event_payload,
#zion_event_ack,
#zion_event_submit {
    position: fixed !important;
    left: -10000px !important;
    top: -10000px !important;
    width: 1px !important;
    height: 1px !important;
    opacity: 0 !important;
    pointer-events: none !important;
    overflow: hidden !important;
}

#zion-editor {
    position: relative;
    width: 100%;
    max-width: none;
    border: 1px solid var(--block-border-color, #d9dee5);
    border-radius: 12px;
    overflow: hidden;
    background: var(--block-background-fill, #ffffff);
    box-shadow: 0 3px 12px rgba(0,0,0,.05);
}

.zion-header,
.zion-row {
    display: grid;
    grid-template-columns:
        65px
        65px
        minmax(0, 1fr)
        65px
        190px;
    width: 100%;
    box-sizing: border-box;
}

.zion-header {
    background: var(--background-fill-secondary, #f5f7fa);
    border-bottom: 1px solid var(--block-border-color, #d9dee5);
    color: var(--body-text-color-subdued, #59636e);
    font-size: 11px;
    font-weight: 800;
    letter-spacing: .04em;
}

.zion-header > div {
    padding: 11px 8px;
    text-align: center;
}

.zion-row {
    min-height: 56px;
    border-bottom: 1px solid var(--block-border-color, #edf0f3);
    background: var(--block-background-fill, #fff);
}

#zion-built-viewer {
    width: 100%;
    border: 1px solid var(--block-border-color, #d9dee5);
    border-radius: 12px;
    overflow: hidden;
    background: var(--block-background-fill, #fff);
    box-shadow: 0 3px 12px rgba(0,0,0,.05);
}

#zion-built-viewer .zion-built-row {
    grid-template-columns: 75px 75px minmax(0, 1fr) 75px !important;
}

#zion-built-viewer .zion-built-text {
    cursor: default;
    resize: vertical;
}

#zion-built-viewer .zion-built-text:focus {
    border-color: transparent;
    box-shadow: none;
}

.zion-built-toolbar {
    align-items: end !important;
    gap: 8px !important;
}

#zion_save_button.zion-save-needed button {
    border: 2px solid #dc2626 !important;
    color: #dc2626 !important;
    box-shadow: 0 0 0 3px rgba(220,38,38,.14) !important;
    animation: zion-save-pulse 1.8s ease-in-out infinite;
}

@keyframes zion-save-pulse {
    50% { box-shadow: 0 0 0 5px rgba(220,38,38,.07); }
}

.zion-toolbar [data-zion-tooltip],
.zion-bottom-nav [data-zion-tooltip],
.zion-note-actions [data-zion-tooltip] {
    position: relative !important;
    overflow: visible !important;
}

.zion-toolbar [data-zion-tooltip]::after,
.zion-bottom-nav [data-zion-tooltip]::after,
.zion-note-actions [data-zion-tooltip]::after {
    content: attr(data-zion-tooltip);
    position: absolute;
    z-index: 1000001;
    left: 50%;
    top: calc(100% + 8px);
    transform: translateX(-50%) translateY(-3px);
    width: max-content;
    max-width: 230px;
    padding: 6px 9px;
    border: 1px solid var(--block-border-color, #334155);
    border-radius: 7px;
    background: var(--body-text-color, #111827);
    color: var(--body-background-fill, #fff);
    box-shadow: 0 8px 22px rgba(15,23,42,.22);
    font-size: 12px;
    font-weight: 600;
    line-height: 1.25;
    white-space: normal;
    pointer-events: none;
    opacity: 0;
    visibility: hidden;
    transition: opacity .15s ease, transform .15s ease;
}

.zion-toolbar [data-zion-tooltip]:hover::after,
.zion-bottom-nav [data-zion-tooltip]:hover::after,
.zion-note-actions [data-zion-tooltip]:hover::after {
    opacity: 1;
    visibility: visible;
    transform: translateX(-50%) translateY(0);
}

.zion-row:hover {
    background: var(--background-fill-secondary, #fafbfd);
}

.zion-row.zion-row-copied {
    background: rgba(34,197,94,.12) !important;
    box-shadow: inset 4px 0 0 #22c55e;
}

.zion-row.zion-row-pasted {
    background: rgba(59,130,246,.12) !important;
    box-shadow: inset 4px 0 0 #3b82f6;
}

.zion-line {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    font-weight: 700;
    color: var(--body-text-color, #46515c);
    background: var(--background-fill-secondary, #fafbfc);
    border-right: 1px solid var(--block-border-color, #edf0f3);
}

.zion-words {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--body-text-color-subdued, #6b7580);
    font-size: 12px;
}

.zion-text {
    position: relative;
    padding: 6px;
}

.zion-row-clipboard {
    position: absolute;
    top: 9px;
    right: 9px;
    z-index: 1000 !important;
    display: flex;
    gap: 5px;
    opacity: 0;
    visibility: hidden;
    transform: translateY(-3px);
    transition: opacity .14s ease, transform .14s ease, visibility .14s;
    pointer-events: auto !important;
    isolation: isolate;
}

.zion-row:hover .zion-row-clipboard,
.zion-row:focus-within .zion-row-clipboard {
    opacity: 1;
    visibility: visible;
    transform: translateY(0);
}

.zion-row-clipboard button {
    position: relative;
    width: 31px;
    height: 31px;
    min-width: 31px;
    padding: 0;
    border: 1px solid var(--block-border-color, #cbd5e1);
    border-radius: 7px;
    background: var(--block-background-fill, rgba(255,255,255,.96));
    color: var(--body-text-color, #27364a);
    box-shadow: 0 4px 12px rgba(15,23,42,.14);
    cursor: pointer;
    font-size: 16px;
    line-height: 27px;
    z-index: 1001 !important;
    pointer-events: auto !important;
    touch-action: manipulation;
    user-select: none;
}

.zion-row-clipboard button:hover {
    border-color: #2563eb;
    background: #eff6ff;
    color: #1d4ed8;
}

.zion-row-clipboard button.zion-clipboard-failed,
.zion-actions button.zion-clipboard-failed,
#zion_copy_note_button button.zion-clipboard-failed {
    border-color: #dc2626 !important;
    background: #fef2f2 !important;
    color: #b91c1c !important;
}

.zion-row-clipboard button::after,
.zion-actions button[data-zion-tooltip]::after {
    content: attr(data-zion-tooltip);
    position: absolute;
    top: calc(100% + 7px);
    right: 0;
    z-index: 50;
    width: max-content;
    max-width: 180px;
    padding: 6px 8px;
    border-radius: 6px;
    background: #172033;
    color: #fff;
    box-shadow: 0 6px 18px rgba(15,23,42,.22);
    font-size: 11px;
    font-weight: 700;
    line-height: 1.2;
    pointer-events: none;
    opacity: 0;
    visibility: hidden;
    transform: translateY(-2px);
    transition: opacity .12s ease, transform .12s ease;
}

.zion-row-clipboard button:hover::after,
.zion-actions button[data-zion-tooltip]:hover::after {
    opacity: 1;
    visibility: visible;
    transform: translateY(0);
}

.zion-editor {
    position: relative;
    z-index: 1;
    display: block;
    width: 100%;
    min-height: 42px;
    box-sizing: border-box;
    resize: vertical;
    border: 1px solid transparent;
    border-radius: 7px;
    outline: none;
    padding: 9px 10px;
    background: transparent;
    font-family:
        "Noto Sans",
        "Noto Sans Ethiopic",
        Arial,
        sans-serif;
    font-size: 14px;
    line-height: 1.5;
    color: var(--body-text-color, #20252b);
}

.zion-editor:hover {
    border-color: var(--block-border-color, #d8dde3);
    background: var(--input-background-fill, #fff);
}

.zion-editor:focus {
    border-color: #6b9dfc;
    background: var(--input-background-fill, #fff);
    box-shadow:
        0 0 0 2px
        rgba(59,130,246,.08);
}

.zion-status {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    font-weight: 800;
}

.status-saved {
    color: #238636;
}

.status-dirty {
    color: #b7791f;
}

.status-new {
    color: #2563eb;
}

.zion-actions {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    min-width: 190px;
    width: 190px;
    box-sizing: border-box;
    overflow: visible;
}

.zion-actions button {
    flex: 0 0 30px;
    width: 30px;
    height: 30px;
    min-width: 30px;
    min-height: 30px;
    padding: 0;
    border: 1px solid var(--button-secondary-border-color, #d5dbe1);
    border-radius: 6px;
    background: var(--button-secondary-background-fill, #fff);
    color: var(--button-secondary-text-color, #46515c);
    cursor: pointer;
    font-size: 15px;
}

.zion-actions button:hover {
    background: var(--button-secondary-background-fill-hover, #f1f4f7);
}

.zion-actions .zion-clipboard-action {
    position: relative;
    z-index: 20;
    opacity: .42;
    pointer-events: auto !important;
    touch-action: manipulation;
    transition: opacity .14s ease, transform .14s ease;
}

.zion-row:hover .zion-actions .zion-clipboard-action,
.zion-row:focus-within .zion-actions .zion-clipboard-action,
.zion-actions .zion-clipboard-action:hover {
    opacity: 1;
    transform: translateY(-1px);
}

.zion-actions .zion-clipboard-action:hover {
    border-color: #2563eb;
    background: #eff6ff;
    color: #1d4ed8;
}

.zion-actions button[data-delete]:hover {
    background: #fff1f1;
    color: #c62828;
}

.zion-footer {
    display: flex;
    flex-wrap: wrap;
    gap: 15px;
    padding: 10px 13px;
    background: var(--background-fill-secondary, #f8fafc);
    color: var(--body-text-color-subdued, #69747f);
    font-size: 12px;
}

.zion-empty {
    min-height: 260px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--block-border-color, #d9dee5);
    border-radius: 12px;
    background: var(--background-fill-secondary, #fafbfd);
    color: var(--body-text-color-subdued, #69747f);
}

.zion-empty-icon {
    font-size: 40px;
    margin-bottom: 10px;
    opacity: .55;
}

.zion-empty-title {
    font-size: 18px;
    font-weight: 700;
    color: var(--body-text-color, #3e4852);
}

.zion-empty-text {
    margin-top: 6px;
    font-size: 13px;
}

#zion_account_button button {
    min-width: 38px !important;
    width: 38px !important;
    padding: 0 !important;
    font-size: 18px !important;
}

.zion-text .zion-editor {
    padding-right: 10px;
}

.zion-mid-nav,
.zion-inline-fullscreen {
    position: absolute;
    z-index: 15;
    width: 42px;
    height: 42px;
    padding: 0;
    border: 1px solid var(--block-border-color, #cbd5e1);
    border-radius: 999px;
    background: var(--block-background-fill, rgba(255,255,255,.94));
    color: var(--body-text-color, #172033);
    box-shadow: 0 7px 22px rgba(15,23,42,.18);
    cursor: pointer;
    font-size: 27px;
    line-height: 38px;
    opacity: .82;
    transition: opacity .15s ease, transform .15s ease;
}

.zion-mid-nav:hover,
.zion-inline-fullscreen:hover {
    opacity: 1;
    transform: scale(1.06);
}

.zion-mid-nav:disabled,
.zion-nav-disabled,
.zion-bottom-nav button:disabled,
.zion-toolbar button:disabled {
    opacity: .3 !important;
    cursor: not-allowed !important;
    pointer-events: none !important;
    box-shadow: none !important;
    transform: none !important;
}

.zion-mid-nav {
    top: 50%;
    transform: translateY(-50%);
}

.zion-mid-nav:hover {
    transform: translateY(-50%) scale(1.06);
}

.zion-mid-previous { left: 10px; }
.zion-mid-next { right: 10px; }

.zion-inline-fullscreen {
    top: 9px;
    right: 9px;
    width: 36px;
    height: 36px;
    font-size: 17px;
    line-height: 32px;
}

.zion-bottom-nav {
    justify-content: center !important;
    align-items: center !important;
    gap: 9px !important;
    margin: 10px 0 18px !important;
    padding: 9px !important;
    border: 1px solid var(--block-border-color, #dfe6ee);
    border-radius: 12px;
    background: var(--block-background-fill, #fff);
    box-shadow: 0 3px 12px rgba(15,23,42,.05);
}

.zion-bottom-nav button {
    width: 54px !important;
    min-width: 54px !important;
    max-width: 54px !important;
    min-height: 40px !important;
    padding: 0 !important;
    border-radius: 999px !important;
    font-weight: 700 !important;
    font-size: 23px !important;
}

#zion_notes_fab {
    position: fixed !important;
    right: 22px !important;
    bottom: 22px !important;
    z-index: 1000010 !important;
    width: auto !important;
    min-width: 54px !important;
}

#zion_notes_fab button {
    min-width: 54px !important;
    height: 54px !important;
    padding: 0 !important;
    border-radius: 999px !important;
    background: linear-gradient(145deg, #ffe66d, #f5ba2e) !important;
    color: #4b3510 !important;
    border: 1px solid #dda51e !important;
    box-shadow: 0 12px 30px rgba(161,113,10,.32) !important;
    font-weight: 800 !important;
    font-size: 23px !important;
}

#zion_notes_fab button:hover {
    transform: translateY(-2px) rotate(-3deg);
    box-shadow: 0 16px 34px rgba(161,113,10,.38) !important;
}

#zion_notes_panel {
    position: fixed !important;
    right: 22px !important;
    bottom: 88px !important;
    z-index: 1000011 !important;
    width: min(390px, calc(100vw - 28px)) !important;
    max-height: min(680px, calc(100vh - 115px)) !important;
    padding: 22px 16px 16px !important;
    overflow: auto !important;
    border: 1px solid #e3c64f !important;
    border-radius: 5px 5px 18px 5px !important;
    background:
        repeating-linear-gradient(
            to bottom,
            #fff8ae 0,
            #fff8ae 31px,
            #eadc82 32px
        ) !important;
    color: #443912 !important;
    box-shadow: 5px 8px 0 rgba(156,119,23,.12),
                0 22px 55px rgba(15,23,42,.28) !important;
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
    transform: none;
}

#zion_notes_panel::before {
    content: "";
    position: absolute;
    top: -7px;
    left: 50%;
    width: 92px;
    height: 24px;
    transform: translateX(-50%) rotate(-2deg);
    background: rgba(232,205,126,.72);
    border-left: 1px solid rgba(133,103,27,.12);
    border-right: 1px solid rgba(133,103,27,.12);
    box-shadow: 0 2px 4px rgba(73,54,10,.09);
    pointer-events: none;
}

.zion-notes-heading {
    align-items: center !important;
    margin-bottom: 2px !important;
}

.zion-notes-heading h3 { margin: 0 !important; }

#zion_notes_panel h3,
#zion_notes_panel label,
#zion_notes_panel .prose,
#zion_notes_panel p {
    color: #443912 !important;
}

#zion_notes_close button {
    min-width: 34px !important;
    width: 34px !important;
    height: 34px !important;
    padding: 0 !important;
    border-radius: 999px !important;
}

.zion-note-actions {
    align-items: end !important;
    gap: 8px !important;
}

#zion_copy_note_button button {
    width: 42px !important;
    min-width: 42px !important;
    height: 42px !important;
    padding: 0 !important;
    border-radius: 999px !important;
    border-color: #bc8716 !important;
    background: rgba(255,255,255,.38) !important;
    color: #60440c !important;
    font-size: 21px !important;
}

#zion_note_text textarea {
    min-height: 150px !important;
    resize: vertical !important;
    border: 1px solid rgba(125,95,20,.22) !important;
    background: rgba(255,255,255,.34) !important;
    color: #332a0b !important;
    font-family: "Segoe Print", "Comic Sans MS", cursive !important;
    font-size: 15px !important;
    line-height: 1.72 !important;
}

#zion_notes_panel button.primary {
    background: #d99a18 !important;
    border-color: #bc7f0c !important;
    color: #fff !important;
}

.zion-shared-notes-title {
    margin: 12px 0 7px;
    font-size: 12px;
    font-weight: 800;
    color: #705716;
    text-transform: uppercase;
    letter-spacing: .05em;
}

.zion-shared-note {
    margin: 0 0 8px;
    padding: 9px 10px;
    border-left: 3px solid #d18c12;
    border-radius: 7px;
    background: rgba(255,255,255,.32);
}

.zion-shared-note-owner {
    margin-bottom: 4px;
    font-size: 11px;
    font-weight: 800;
    color: #8a5b0b;
}

.zion-shared-note-text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 13px;
    line-height: 1.45;
}

#zion_account_status {
    font-size: 12px;
    color: var(--body-text-color-subdued, #64748b);
    margin-top: -10px;
    margin-bottom: 8px;
}

#zion_account_modal {
    display: none !important;
    position: fixed !important;
    inset: 0 !important;
    z-index: 1000000 !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 18px !important;
    background: rgba(15,23,42,.58) !important;
    backdrop-filter: blur(5px);
}

#zion_account_modal.zion-open {
    display: flex !important;
}

#zion_account_modal .zion-account-card {
    width: min(820px, 96vw) !important;
    max-height: 90vh !important;
    overflow-y: auto !important;
    padding: 22px !important;
    border: 1px solid var(--block-border-color, rgba(148,163,184,.35)) !important;
    border-radius: 18px !important;
    background: var(--body-background-fill, #fff) !important;
    color: var(--body-text-color, #1f2937) !important;
    box-shadow: 0 28px 90px rgba(15,23,42,.35) !important;
}

#zion_account_modal .zion-account-heading {
    align-items: center !important;
    margin-bottom: 4px !important;
}

#zion_account_close button {
    width: 38px !important;
    min-width: 38px !important;
    height: 38px !important;
    padding: 0 !important;
    border-radius: 50% !important;
    font-size: 22px !important;
}

#zion_account_modal h3 {
    margin-top: 14px !important;
    color: var(--body-text-color, #1e3a5f);
}

#zion_account_modal .tab-nav {
    border-bottom-color: var(--block-border-color, #dfe6ee) !important;
}

#zion_file_access_checks {
    padding: 12px !important;
    border: 1px solid var(--block-border-color, #dfe6ee) !important;
    border-radius: 12px !important;
    background: var(--background-fill-secondary, #f8fafc) !important;
}

#zion_file_access_checks .wrap,
#zion_file_access_checks .container {
    display: grid !important;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)) !important;
    gap: 7px 12px !important;
    max-height: 300px !important;
    overflow-y: auto !important;
}

#zion_file_access_checks label {
    margin: 0 !important;
    padding: 8px 10px !important;
    border-radius: 8px !important;
    background: var(--block-background-fill, #fff) !important;
}

form button[type="submit"] {
    position: relative;
}

form button[type="submit"][disabled]::after,
form button[type="submit"][aria-busy="true"]::after {
    content: "";
    display: inline-block;
    width: 14px;
    height: 14px;
    margin-left: 9px;
    vertical-align: -2px;
    border: 2px solid currentColor;
    border-right-color: transparent;
    border-radius: 50%;
    animation: zion-login-spin .7s linear infinite;
}

@keyframes zion-login-spin {
    to { transform: rotate(360deg); }
}

.multiline-dialog {
    position: fixed;
    inset: 0;
    z-index: 999999;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0,0,0,.35);
}

.multiline-dialog-card {
    width: min(500px, 90vw);
    background: var(--body-background-fill, white);
    color: var(--body-text-color, #1f2937);
    border-radius: 13px;
    box-shadow: 0 20px 60px rgba(0,0,0,.25);
    padding: 22px;
}

.multiline-dialog-title {
    font-size: 18px;
    font-weight: 750;
    margin-bottom: 8px;
}

.multiline-dialog-text {
    color: var(--body-text-color-subdued, #5f6974);
    line-height: 1.55;
    margin-bottom: 18px;
}

.multiline-dialog-buttons {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
}

.multiline-dialog-buttons button {
    padding: 9px 16px;
    border-radius: 7px;
    border: 1px solid #d5dbe1;
    background: white;
    cursor: pointer;
}

.multiline-dialog-buttons .confirm {
    background: #2563eb;
    color: white;
    border-color: #2563eb;
}

.zion-insert-input {
    width: 100%;
    box-sizing: border-box;
    margin: 0 0 18px;
    padding: 10px 11px;
    border: 1px solid #cfd8e3;
    border-radius: 8px;
    outline: none;
    font: inherit;
}

.zion-insert-input:focus {
    border-color: #2563eb;
    box-shadow: 0 0 0 3px rgba(37,99,235,.12);
}

@media(max-width: 850px) {

    .zion-header,
    .zion-row {
        grid-template-columns:
            58px
            65px
            minmax(260px, 1fr)
            55px
            140px;
    }

    .zion-actions {
        min-width: 140px;
        width: 140px;
    }

    .zion-actions button {
        width: 24px;
        height: 24px;
    }
}

/* =========================================================
   FULL SCREEN EDITOR
   ========================================================= */

#zion_editor_container.zion-fullscreen {
    position: fixed !important;
    inset: 0 !important;
    z-index: 999999 !important;

    width: 100vw !important;
    height: 100vh !important;

    max-width: none !important;
    margin: 0 !important;
    padding: 12px !important;

    background: var(--body-background-fill, #ffffff) !important;

    overflow: auto !important;

    box-sizing: border-box !important;
}

/* Make the actual editor fill fullscreen width */

#zion_editor_container.zion-fullscreen #zion-editor {
    width: 100% !important;
    max-width: none !important;
}

/* Keep actions visible */

#zion_editor_container.zion-fullscreen .zion-actions {
    min-width: 190px !important;
    width: 190px !important;
}

/* Fullscreen rows */

#zion_editor_container.zion-fullscreen .zion-header,
#zion_editor_container.zion-fullscreen .zion-row {
    width: 100% !important;
    grid-template-columns:
        70px
        70px
        minmax(0, 1fr)
        70px
        190px !important;
}

/* Fullscreen editor text area */

#zion_editor_container.zion-fullscreen .zion-editor {
    font-size: 15px !important;
}

.zion-paste-capture {
    min-height: 130px;
    resize: vertical;
}

#zion_editor_container.zion-fullscreen .zion-mid-nav,
#zion_editor_container.zion-fullscreen .zion-inline-fullscreen {
    position: fixed;
    z-index: 1000000;
}

#zion_editor_container.zion-fullscreen .zion-mid-nav {
    top: 50vh;
}

#zion_editor_container.zion-fullscreen .zion-inline-fullscreen {
    top: 16px;
    right: 20px;
}

#zion_editor_container.zion-fullscreen .zion-inline-fullscreen {
    font-size: 0;
}

#zion_editor_container.zion-fullscreen .zion-inline-fullscreen::after {
    content: "×";
    font-size: 22px;
    font-weight: 800;
}

/* Prevent background scrolling */

body.zion-no-scroll {
    overflow: hidden !important;
}
"""


# ============================================================
# 42. JAVASCRIPT
# ============================================================

JS = r"""
() => {

    if (window.__zionEditorControlsLoaded) {
        return;
    }

    console.log(
        "ZION Smart Editor V4.4.1 controls loaded"
    );

    const zionTooltips = {
        zion_go_button: "Go to the entered row",
        zion_previous_button: "Previous page",
        zion_next_button: "Next page",
        zion_refresh_button: "Refresh the current page",
        zion_fullscreen_button: "Toggle full-screen editor",
        zion_save_button: "Save pending changes to GCS",
        zion_build_button: "Build the complete final text file",
        zion_help_button: "Open editor help",
        zion_account_button: "Account, files, users, and settings",
        zion_bottom_previous_button: "Previous page",
        zion_bottom_fullscreen_button: "Toggle full-screen editor",
        zion_bottom_next_button: "Next page",
        zion_notes_fab: "Open global sticky notes",
        zion_copy_note_button: "Copy note text"
    };

    function refineApplicationChrome() {
        for (const [id, title] of Object.entries(zionTooltips)) {
            const wrapper = document.getElementById(id);
            const button = wrapper && wrapper.querySelector("button");
            if (wrapper) wrapper.setAttribute("data-zion-tooltip", title);
            if (button && button.title !== title) button.title = title;
        }

        document.querySelectorAll("footer a, footer button").forEach(link => {
            const label = link.textContent.trim().toLowerCase();
            if (label.includes("use via api") || label.includes("built with gradio")) {
                link.style.display = "none";
            }
        });
    }

    function setSaveAttention(forceDirty = null) {
        const editor = document.getElementById("zion-editor");
        const dirty = forceDirty === null
            ? Boolean(
                editor && (
                    editor.dataset.unsaved === "1"
                    || editor.querySelector(".status-dirty, .status-new")
                )
              )
            : Boolean(forceDirty);

        const saveWrapper = document.getElementById("zion_save_button");
        if (saveWrapper) saveWrapper.classList.toggle("zion-save-needed", dirty);

    }

    function updateNavigationAvailability() {
        const editor = document.getElementById("zion-editor");
        const start = editor ? Number(editor.dataset.start || 1) : 1;
        const end = editor ? Number(editor.dataset.end || 0) : 0;
        const total = editor ? Number(editor.dataset.total || 0) : 0;
        const atStart = !editor || total <= 0 || start <= 1;
        const atEnd = !editor || total <= 0 || end >= total;

        const setDisabled = (selector, disabled) => {
            document.querySelectorAll(selector).forEach(control => {
                control.disabled = disabled;
                control.setAttribute("aria-disabled", disabled ? "true" : "false");
                control.classList.toggle("zion-nav-disabled", disabled);
            });
        };

        setDisabled(
            "#zion_previous_button button, #zion_bottom_previous_button button, [data-zion-nav='previous']",
            atStart
        );
        setDisabled(
            "#zion_next_button button, #zion_bottom_next_button button, [data-zion-nav='next']",
            atEnd
        );
    }

    setInterval(() => {
        refineApplicationChrome();
        setSaveAttention();
        updateNavigationAvailability();
    }, 700);

    window.setTimeout(() => {
        refineApplicationChrome();
        setSaveAttention();
        updateNavigationAvailability();
    }, 0);

    // ========================================================
    // EVENT CHANNEL
    // ========================================================

    function getPayloadBox() {

        const selectors = [

            "#zion_event_payload textarea",

            "#zion_event_payload input",

            "#zion_event_payload [contenteditable='true']"

        ];


        for (
            const selector
            of selectors
        ) {

            const box =
                document.querySelector(
                    selector
                );


            if (box) {

                return box;

            }

        }


        const wrapper =
            document.getElementById(
                "zion_event_payload"
            );


        if (wrapper) {

            return (
                wrapper.querySelector(
                    "textarea"
                )
                ||
                wrapper.querySelector(
                    "input"
                )
                ||
                wrapper.querySelector(
                    "[contenteditable='true']"
                )
            );

        }


        return null;

    }

    function getAckBox() {
        return document.querySelector(
            "#zion_event_ack textarea, "
            + "#zion_event_ack input, "
            + "#zion_event_ack [contenteditable='true']"
        );
    }

    function getEventSubmitButton() {
        return (
            document.querySelector("#zion_event_submit button")
            || document.getElementById("zion_event_submit")
        );
    }

    function setNativeBoxValue(box, value) {
        const prototype = Object.getPrototypeOf(box);
        const descriptor = Object.getOwnPropertyDescriptor(
            prototype,
            "value"
        );

        if (descriptor && descriptor.set) {
            descriptor.set.call(box, value);
        } else {
            box.value = value;
        }
    }

    const pendingEditorEvents = [];
    let activeEditorEventId = null;

    function pumpEditorEvents() {
        if (activeEditorEventId || !pendingEditorEvents.length) {
            return;
        }

        const box = getPayloadBox();
        const submitButton = getEventSubmitButton();
        if (!box || !submitButton) {
            return;
        }

        const next = pendingEditorEvents.shift();
        activeEditorEventId = next.event_id;
        setNativeBoxValue(box, JSON.stringify(next));

        const inputEvent = typeof InputEvent === "function"
            ? new InputEvent("input", {
                bubbles: true,
                inputType: "insertText",
                data: box.value
            })
            : new Event("input", {bubbles: true});

        box.dispatchEvent(inputEvent);
        box.dispatchEvent(new Event("change", {bubbles: true}));

        // Give Gradio's frontend two render frames to record the Textbox value
        // before triggering the real server-bound Button callback.
        const dispatch = () => submitButton.click();
        if (typeof window.requestAnimationFrame === "function") {
            window.requestAnimationFrame(() => {
                window.requestAnimationFrame(dispatch);
            });
        } else {
            window.setTimeout(dispatch, 0);
        }
    }

    function acknowledgeEditorEvent() {
        if (!activeEditorEventId) {
            pumpEditorEvents();
            return;
        }

        const ackBox = getAckBox();
        if (!ackBox || String(ackBox.value || "") !== activeEditorEventId) {
            return;
        }

        activeEditorEventId = null;
        pumpEditorEvents();
    }

    window.setInterval(acknowledgeEditorEvent, 120);


    // ========================================================
    // SEND EVENT
    // ========================================================

    function sendEvent(payload) {

        if (["edit", "multiline_confirmed", "insert", "delete"].includes(payload.type)) {
            setSaveAttention(true);
        }

        pendingEditorEvents.push({
            ...payload,
            event_id:
                Date.now().toString(36)
                + "-"
                + Math.random().toString(36).slice(2)
        });

        pumpEditorEvents();

    }


    // ========================================================
    // NORMALIZE CLIPBOARD
    // ========================================================

function normalizeClipboard(raw) {

    let normalized =
        String(raw ?? "");

    normalized =
        normalized.replace(
            /\r\n/g,
            "\n"
        );

    normalized =
        normalized.replace(
            /\r/g,
            "\n"
        );

    normalized =
        normalized.replace(
            /[\u2028\u2029\u000b\u000c]/g,
            "\n"
        );

    const rawLines =
        normalized.split("\n");

    // Empty/whitespace clipboard lines are not dataset rows. This guarantees
    // that copying 10 populated lines inserts exactly 10 rows.
    return rawLines.filter(
        line => line.trim() !== ""
    );
}

    async function copyTextToClipboard(text, selectionTarget = null) {
        const value = String(text ?? "");

        // Prefer the modern API because it does not move focus or disturb the
        // user's current selection. Keep selection copy as a compatibility
        // fallback for HTTP and older browsers.
        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(value);
                return true;
            } catch (error) {
                console.debug("Clipboard API write was unavailable", error);
            }
        }

        if (selectionTarget) {
            selectionTarget.focus();
            if (typeof selectionTarget.select === "function") {
                selectionTarget.select();
            }
            if (typeof selectionTarget.setSelectionRange === "function") {
                selectionTarget.setSelectionRange(0, value.length);
            }
            try {
                if (document.execCommand("copy")) return true;
            } catch (error) {
                console.debug("Native selection copy was unavailable", error);
            }
        }

        const fallback = document.createElement("textarea");
        fallback.value = value;
        fallback.setAttribute("readonly", "");
        fallback.style.position = "fixed";
        fallback.style.left = "-10000px";
        document.body.appendChild(fallback);
        fallback.select();
        const copied = document.execCommand("copy");
        fallback.remove();
        if (!copied) throw new Error("Clipboard copy was blocked.");
        return true;
    }

    function flashClipboardButton(button, success) {
        if (!button) return;
        const original = button.innerHTML;
        button.innerHTML = success ? "✓" : "!";
        button.classList.toggle("zion-clipboard-failed", !success);
        window.setTimeout(() => {
            if (!button.isConnected) return;
            button.innerHTML = original;
            button.classList.remove("zion-clipboard-failed");
        }, 950);
    }

    function pasteTextIntoRow(row, raw) {
        if (!row) return false;
        const editor = row.querySelector("[data-editor]");
        const lines = normalizeClipboard(raw);
        if (!editor || !lines.length) return false;

        // The pending debounce contains the pre-paste value and must not be
        // allowed to overwrite the first pasted line after the paste finishes.
        clearTimeout(editor._zionTimer);
        editor._zionTimer = null;

        if (lines.length === 1) {
            editor.value = lines[0];
            updateWords(row);
            markDirty(row);
            sendEvent({
                type: "edit",
                row_id: row.dataset.rowId,
                line: Number(row.dataset.line),
                text: lines[0]
            });
        } else {
            showMultilineDialog(row, lines);
        }
        return true;
    }

    function markClipboardRow(row, kind) {
        if (!row) return;
        row.classList.remove("zion-row-copied", "zion-row-pasted");
        row.classList.add(kind === "copy" ? "zion-row-copied" : "zion-row-pasted");
        window.setTimeout(() => {
            if (row.isConnected) {
                row.classList.remove("zion-row-copied", "zion-row-pasted");
            }
        }, 1500);
    }

    function showClipboardPasteDialog(row) {
        const old = document.querySelector(".zion-clipboard-dialog");
        if (old) old.remove();

        const overlay = document.createElement("div");
        overlay.className = "multiline-dialog zion-clipboard-dialog";
        overlay.innerHTML = `
            <div class="multiline-dialog-card">
                <div class="multiline-dialog-title">Paste into row ${row.dataset.line}</div>
                <div class="multiline-dialog-text">
                    Your browser blocked direct clipboard reading. Press Ctrl+V here,
                    then choose Paste. Multiline and empty-row rules remain unchanged.
                </div>
                <textarea class="zion-insert-input zion-paste-capture"
                    placeholder="Press Ctrl+V here"></textarea>
                <div class="multiline-dialog-buttons">
                    <button type="button" class="cancel">CANCEL</button>
                    <button type="button" class="confirm">PASTE</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        const input = overlay.querySelector(".zion-paste-capture");
        const submit = () => {
            if (!pasteTextIntoRow(row, input.value)) {
                input.focus();
                return;
            }
            markClipboardRow(row, "paste");
            overlay.remove();
        };
        overlay.querySelector(".cancel").addEventListener("click", () => overlay.remove());
        overlay.querySelector(".confirm").addEventListener("click", submit);
        overlay.addEventListener("click", event => {
            if (event.target === overlay) overlay.remove();
        });
        input.addEventListener("keydown", event => {
            if (event.key === "Escape") overlay.remove();
        });
        input.focus();
    }

    async function handleRowCopy(event, button) {
        event.preventDefault();
        event.stopPropagation();
        const row = button.closest(".zion-row");
        const editor = row && row.querySelector("[data-editor]");
        try {
            await copyTextToClipboard(editor ? editor.value : "", editor);
            markClipboardRow(row, "copy");
            flashClipboardButton(button, true);
        } catch (error) {
            console.error("ZION row copy failed", error);
            flashClipboardButton(button, false);
        }
    }

    async function handleRowPaste(event, button) {
        event.preventDefault();
        event.stopPropagation();
        const row = button.closest(".zion-row");
        const editor = row && row.querySelector("[data-editor]");
        if (editor) editor.focus();

        try {
            if (!window.isSecureContext) {
                throw new Error("Direct clipboard reading requires HTTPS.");
            }
            if (!navigator.clipboard || !navigator.clipboard.readText) {
                throw new Error("Clipboard reading is unavailable.");
            }
            const raw = await navigator.clipboard.readText();
            if (!pasteTextIntoRow(row, raw)) {
                throw new Error("The clipboard contains no populated lines.");
            }
            markClipboardRow(row, "paste");
            flashClipboardButton(button, true);
        } catch (error) {
            console.debug("Opening safe paste fallback", error);
            showClipboardPasteDialog(row);
        }
    }

    async function handleNoteCopy(event, button) {
        event.preventDefault();
        event.stopPropagation();
        const noteBox = document.querySelector(
            "#zion_note_text textarea, #zion_note_text input"
        );
        try {
            await copyTextToClipboard(noteBox ? noteBox.value : "", noteBox);
            flashClipboardButton(button, true);
        } catch (error) {
            console.error("ZION note copy failed", error);
            flashClipboardButton(button, false);
        }
    }

    // Delegate clipboard actions from the document.  Rows are replaced after
    // every structural refresh, so direct button bindings were frequently
    // lost or were not attached yet when users clicked during a slow load.
    document.addEventListener(
        "click",
        event => {
            const copyButton = event.target.closest("[data-copy-row]");
            if (copyButton) {
                handleRowCopy(event, copyButton);
                return;
            }

            const pasteButton = event.target.closest("[data-paste-row]");
            if (pasteButton) {
                handleRowPaste(event, pasteButton);
                return;
            }

            const noteButton = event.target.closest(
                "#zion_copy_note_button button"
            );
            if (noteButton) {
                handleNoteCopy(event, noteButton);
            }
        },
        true
    );

    // ========================================================
    // WORD COUNT
    // ========================================================

    function updateWords(row) {

        const editor =
            row.querySelector(
                "[data-editor]"
            );


        const words =
            row.querySelector(
                "[data-words]"
            );


        if (
            !editor
            ||
            !words
        ) {

            return;

        }


        const text =
            editor.value.trim();


        if (!text) {

            words.textContent =
                "0";

            return;

        }


        words.textContent =
            text
                .split(/\s+/)
                .filter(Boolean)
                .length
                .toLocaleString();

    }


    // ========================================================
    // DIRTY STATUS
    // ========================================================

    function markDirty(row) {

        const status =
            row.querySelector(
                "[data-status]"
            );


        if (!status) {

            return;

        }


        if (
            status.textContent.trim()
            ===
            "+"
        ) {

            status.classList.remove(
                "status-saved",
                "status-dirty"
            );

            status.classList.add(
                "status-new"
            );

            return;

        }


        status.textContent =
            "✎";


        status.classList.remove(
            "status-saved",
            "status-new"
        );


        status.classList.add(
            "status-dirty"
        );

    }


    // ========================================================
    // MULTILINE CONFIRMATION
    // ========================================================

    function showMultilineDialog(
        row,
        lines
    ) {

        const old =
            document.querySelector(
                ".multiline-dialog"
            );


        if (old) {

            old.remove();

        }


        const count =
            lines.length;


        const additional =
            Math.max(
                0,
                count - 1
            );


        const operationText =
            "Replace 1 row and add "
            +
            additional.toLocaleString()
            +
            (
                additional === 1
                ? " row below"
                : " rows below"
            );


        const overlay =
            document.createElement(
                "div"
            );


        overlay.className =
            "multiline-dialog";


        overlay.innerHTML = `

            <div
                class="multiline-dialog-card"
            >

                <div
                    class="multiline-dialog-title"
                >
                    Multiple lines detected
                </div>


                <div
                    class="multiline-dialog-text"
                >

                    You pasted
                    <strong>
                        ${count.toLocaleString()}
                    </strong>
                    lines.

                    <br><br>

                    <strong>
                        ${operationText}
                    </strong>

                    <br><br>

                    The first pasted line replaces
                    the currently selected row.

                    <br>

                    The remaining
                    <strong>
                        ${additional.toLocaleString()}
                    </strong>
                    line(s) are inserted directly
                    below it.

                </div>


                <div
                    class="multiline-dialog-buttons"
                >

                    <button
                        type="button"
                        class="cancel"
                    >
                        CANCEL
                    </button>


                    <button
                        type="button"
                        class="confirm"
                    >
                        ${operationText}
                    </button>

                </div>

            </div>

        `;


        document.body.appendChild(
            overlay
        );


        overlay
            .querySelector(
                ".cancel"
            )
            .addEventListener(
                "click",
                () => {

                    overlay.remove();

                }
            );


        overlay
            .querySelector(
                ".confirm"
            )
            .addEventListener(
                "click",
                () => {

                    overlay.remove();


                    const rowId =
                        row.dataset.rowId;


                   sendEvent({
                    type: "multiline_confirmed",
                    row_id: rowId,
                    line: Number(row.dataset.line),
                    lines: lines
                });

                }
            );

    }


    // ========================================================
    // INPUT / TYPING
    // ========================================================

    document.addEventListener(
        "input",
        event => {

            const editor =
                event.target.closest(
                    "[data-editor]"
                );


            if (!editor) {

                return;

            }


            const row =
                editor.closest(
                    ".zion-row"
                );


            if (!row) {

                return;

            }


            updateWords(
                row
            );


            markDirty(
                row
            );


            if (
                editor.value.includes(
                    "\n"
                )
            ) {

                return;

            }


            clearTimeout(
                editor._zionTimer
            );


            editor._zionTimer =
                setTimeout(
                    () => {

                        editor._zionTimer = null;

                        sendEvent(
                            {

                                type:
                                    "edit",

                                row_id:
                                    row.dataset.rowId,

                                line:
                                    Number(row.dataset.line),

                                text:
                                    editor.value

                            }
                        );

                    },
                    500
                );

        },
        true
    );


    // ========================================================
    // PASTE
    // ========================================================

    document.addEventListener(
        "paste",
        event => {

            const editor =
                event.target.closest(
                    "[data-editor]"
                );


            if (!editor) {

                return;

            }


            const clipboard =
                event.clipboardData;


            if (!clipboard) {

                return;

            }


            const raw =
                clipboard.getData(
                    "text"
                );


            if (
                raw === null
                ||
                raw === undefined
            ) {

                return;

            }


            const lines =
                normalizeClipboard(
                    raw
                );


            if (!lines.length) {

                // Whitespace-only clipboard content is intentionally ignored.
                // Prevent the browser's default paste so it cannot leave an
                // unsent newline in the textarea while the database is unchanged.
                event.preventDefault();
                event.stopPropagation();

                return;

            }


            const row =
                editor.closest(
                    ".zion-row"
                );


            if (!row) {

                return;

            }


            const rowId =
                row.dataset.rowId;


            event.preventDefault();

            event.stopPropagation();

            // The paste event replaces the whole row, so discard any older
            // debounced edit that still contains the pre-paste value.
            clearTimeout(editor._zionTimer);
            editor._zionTimer = null;


            // ------------------------------------------------
            // ONE LINE
            // ------------------------------------------------

            if (
                lines.length === 1
            ) {

                const newText =
                    lines[0];


                editor.value =
                    newText;


                updateWords(
                    row
                );


                markDirty(
                    row
                );


                sendEvent(
                    {

                        type:
                            "edit",

                        row_id:
                            rowId,

                        line:
                            Number(row.dataset.line),

                        text:
                            newText

                    }
                );


                return;

            }


            // ------------------------------------------------
            // MULTIPLE LINES
            // ------------------------------------------------

            showMultilineDialog(
                row,
                lines
            );

        },
        true
    );


    // ========================================================
    // ROW ACTIONS
    // ========================================================

    function showInsertDialog(row, direction) {

        const old = document.querySelector(".insert-dialog");
        if (old) old.remove();

        const label = direction === "above" ? "above" : "below";
        const overlay = document.createElement("div");
        overlay.className = "multiline-dialog insert-dialog";
        overlay.innerHTML = `
            <div class="multiline-dialog-card">
                <div class="multiline-dialog-title">Insert row ${label}</div>
                <div class="multiline-dialog-text">
                    Add the text for the new row. Empty rows are not allowed.
                </div>
                <input class="zion-insert-input" type="text" placeholder="New row text" />
                <div class="multiline-dialog-buttons">
                    <button type="button" class="cancel">CANCEL</button>
                    <button type="button" class="confirm">INSERT</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        const input = overlay.querySelector(".zion-insert-input");
        const submit = () => {
            const text = input.value.trim();
            if (!text) {
                input.focus();
                return;
            }
            overlay.remove();
            sendEvent({
                type: "insert",
                row_id: row.dataset.rowId,
                line: Number(row.dataset.line),
                direction: direction,
                text: text
            });
        };

        overlay.querySelector(".cancel").addEventListener("click", () => overlay.remove());
        overlay.querySelector(".confirm").addEventListener("click", submit);
        input.addEventListener("keydown", event => {
            if (event.key === "Enter") submit();
            if (event.key === "Escape") overlay.remove();
        });
        input.focus();
    }

    document.addEventListener(
        "click",
        event => {

            const above =
                event.target.closest(
                    "[data-add-above]"
                );


            const below =
                event.target.closest(
                    "[data-add-below]"
                );


            const deleteButton =
                event.target.closest(
                    "[data-delete]"
                );


            if (
                !above
                &&
                !below
                &&
                !deleteButton
            ) {

                return;

            }


            const button =
                above
                ||
                below
                ||
                deleteButton;


            const row =
                button.closest(
                    ".zion-row"
                );


            if (!row) {

                return;

            }


            const rowId =
                row.dataset.rowId;


            if (above) {
                showInsertDialog(row, "above");
                return;

            }


            if (below) {
                showInsertDialog(row, "below");
                return;

            }


            if (deleteButton) {

                const line =
                    row.dataset.line;


                const confirmed =
                    window.confirm(
                        "Delete row "
                        +
                        line
                        +
                        "?"
                    );


                if (!confirmed) {

                    return;

                }

                // Clicking delete immediately after typing used to leave a
                // timer behind that could re-submit the old row after it was
                // deleted. Deletion is authoritative for this row.
                const rowEditor = row.querySelector("[data-editor]");
                if (rowEditor) {
                    clearTimeout(rowEditor._zionTimer);
                    rowEditor._zionTimer = null;
                }


                sendEvent(
                    {

                        type:
                            "delete",

                        row_id:
                            rowId,

                        line:
                            Number(row.dataset.line)

                    }
                );

            }

        },
        true
    );

        // ========================================================
    // FULL SCREEN EDITOR
    // ========================================================

    function toggleEditorFullscreen() {

        const container =
            document.getElementById(
                "zion_editor_container"
            );

        if (!container) {

            return;

        }

        const active =
            container.classList.toggle(
                "zion-fullscreen"
            );

        document.body.classList.toggle(
            "zion-no-scroll",
            active
        );

        const topButton = document.querySelector("#zion_fullscreen_button button");
        if (topButton) topButton.innerText = active ? "×" : "⛶";

        const bottomButton = document.querySelector(
            "#zion_bottom_fullscreen_button button"
        );
        if (bottomButton) {
            bottomButton.innerText = active ? "×" : "⛶";
        }

    }

    function showHelpDialog() {

        const old = document.querySelector(".zion-help-dialog");
        if (old) old.remove();

        const overlay = document.createElement("div");
        overlay.className = "multiline-dialog zion-help-dialog";
        overlay.innerHTML = `
            <div class="multiline-dialog-card">
                <div class="multiline-dialog-title">Editor guide</div>
                <div class="multiline-dialog-text">
                    <strong>Edit:</strong> click a text cell and type.<br><br>
                    <strong>Row clipboard:</strong> hover a row and use its Copy or
                    Paste icon in the upper-right corner.<br><br>
                    <strong>Paste:</strong> the first line replaces the selected row;
                    later non-empty lines are inserted directly below it.<br><br>
                    <strong>Insert:</strong> use the row arrows and enter the text for
                    the new row. Empty rows are not allowed.<br><br>
                    <strong>Navigate:</strong> enter a row number and choose Go, or use
                    Previous and Next above, beside, or below the editor. Each file
                    remembers your last page.<br><br>
                    <strong>Notes:</strong> use the floating sticky-note button to keep a
                    global personal reminder. Its Copy icon copies your complete note.
                    Administrator notes are read-only to
                    everyone except the administrator who owns the note.<br><br>
                    Save stores the patch; Build creates the final file.
                </div>
                <div class="multiline-dialog-buttons">
                    <button type="button" class="confirm">DONE</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        overlay.querySelector(".confirm").addEventListener("click", () => overlay.remove());
        overlay.addEventListener("click", event => {
            if (event.target === overlay) overlay.remove();
        });
    }


    document.addEventListener(
        "click",
        event => {

            const accountModal = document.getElementById(
                "zion_account_modal"
            );

            const notesPanel = document.getElementById(
                "zion_notes_panel"
            );

            if (event.target.closest("#zion_notes_fab")) {
                return;
            }

            if (event.target.closest("#zion_notes_close")) {
                return;
            }

            const pageNavigator = event.target.closest("[data-zion-nav]");
            if (pageNavigator) {
                if (pageNavigator.disabled || pageNavigator.classList.contains("zion-nav-disabled")) {
                    return;
                }
                const target = document.querySelector(
                    pageNavigator.dataset.zionNav === "previous"
                        ? "#zion_previous_button button"
                        : "#zion_next_button button"
                );
                if (target) target.click();
                return;
            }

            if (event.target.closest("#zion_account_button")) {
                const notesClose = document.querySelector("#zion_notes_close button");
                if (notesPanel && notesClose) notesClose.click();
                if (accountModal) accountModal.classList.add("zion-open");
                return;
            }

            if (event.target.closest("#zion_account_close")) {
                if (accountModal) accountModal.classList.remove("zion-open");
                return;
            }

            if (event.target.closest("#zion_logout_button")) {
                event.preventDefault();
                window.location.assign("./logout");
                return;
            }

            const helpButton = event.target.closest(
                "#zion_help_button"
            );

            if (helpButton) {
                showHelpDialog();
                return;
            }

            const button =
                event.target.closest(
                    "#zion_fullscreen_button, #zion_bottom_fullscreen_button, [data-zion-fullscreen]"
                );

            if (!button) {

                return;

            }

            toggleEditorFullscreen();

        },
        true
    );


    document.addEventListener(
        "keydown",
        event => {

            if (
                event.key === "Escape"
            ) {

                const accountModal = document.getElementById(
                    "zion_account_modal"
                );
                if (accountModal) accountModal.classList.remove("zion-open");

                const notesPanel = document.getElementById(
                    "zion_notes_panel"
                );
                const notesClose = document.querySelector("#zion_notes_close button");
                if (notesPanel && notesClose) notesClose.click();

                const container =
                    document.getElementById(
                        "zion_editor_container"
                    );

                if (
                    container
                    &&
                    container.classList.contains(
                        "zion-fullscreen"
                    )
                ) {

                    toggleEditorFullscreen();

                }

            }

        },
        true
    );


    // ========================================================
    // CTRL/CMD + S
    // ========================================================

    document.addEventListener(
        "keydown",
        event => {

            if (
                !(
                    event.ctrlKey
                    ||
                    event.metaKey
                )
            ) {

                return;

            }


            if (
                event.key.toLowerCase()
                !==
                "s"
            ) {

                return;

            }


            event.preventDefault();


            const save = document.querySelector(
                "#zion_save_button button"
            );


            if (save) {

                save.click();

            }

        },
        true
    );

    window.__zionEditorControlsLoaded = true;

}
"""


# ============================================================
# 43. BUILD APPLICATION
# ============================================================

APP_THEME = gr.themes.Soft(
    primary_hue="blue",
    secondary_hue="slate",
    neutral_hue="slate"
)

# Gradio currently has releases that warn styling is moving to launch(), even
# though their launch() implementation does not yet accept those arguments.
# Select the supported API at runtime so both Gradio 5 variants and Gradio 6
# can start successfully.
_LAUNCH_PARAMETERS = inspect.signature(gr.Blocks.launch).parameters
_LAUNCH_SUPPORTS_STYLING = all(
    name in _LAUNCH_PARAMETERS for name in ("theme", "css", "js")
)
BLOCKS_STYLE_KWARGS = {} if _LAUNCH_SUPPORTS_STYLING else {
    "theme": APP_THEME,
    "css": CSS,
    "js": JS,
}
LAUNCH_STYLE_KWARGS = {
    "theme": APP_THEME,
    "css": CSS,
    "js": JS,
} if _LAUNCH_SUPPORTS_STYLING else {}
LAUNCH_FOOTER_KWARGS = (
    {"footer_links": ["settings"]}
    if "footer_links" in _LAUNCH_PARAMETERS
    else {}
)

if not _LAUNCH_SUPPORTS_STYLING:
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r"The '(theme|css|js)' parameter in the Blocks constructor.*"
    )

with gr.Blocks(

    title=
        "ZION Smart GCS Editor V4.4.1",

    **BLOCKS_STYLE_KWARGS

) as app:


    # ========================================================
    # HEADER
    # ========================================================

    gr.Markdown(
        """
# ZION Smart GCS Line Editor V4.4.1
"""
    )

    with gr.Group(
        elem_id="zion_account_modal",
        elem_classes=["zion-account-modal"]
    ):
        with gr.Column(elem_classes=["zion-account-card"]):
            with gr.Row(elem_classes=["zion-account-heading"]):
                gr.Markdown("## Account & file management")
                account_close_button = gr.Button(
                    "×", elem_id="zion_account_close", scale=0
                )
            account_status = gr.Markdown("", elem_id="zion_account_status")

            with gr.Tabs():
                with gr.Tab("My account"):
                    gr.Markdown("Change your login password or securely end this session.")
                    current_password = gr.Textbox(label="Current password", type="password")
                    new_password = gr.Textbox(label="New password", type="password")
                    confirm_password = gr.Textbox(label="Confirm new password", type="password")
                    with gr.Row():
                        change_password_button = gr.Button(
                            "Change password", variant="primary"
                        )
                        logout_button = gr.Button(
                            "Log out", variant="stop", elem_id="zion_logout_button"
                        )
                    password_status = gr.Markdown("")

                with gr.Tab("Part files"):
                    gr.Markdown(
                        f"Files are created only in `gs://{BUCKET_NAME}/{GCS_FOLDER}`. "
                        "The next `part-000000.txt` name is selected automatically."
                    )
                    dataset_upload = gr.File(
                        label="Select the text content for the next part",
                        file_types=[".txt", ".text"],
                        type="filepath"
                    )
                    with gr.Row():
                        upload_dataset_button = gr.Button(
                            "Create next part", variant="primary"
                        )
                        refresh_folder_button = gr.Button(
                            "Refresh GCS folder", variant="secondary"
                        )
                    upload_dataset_status = gr.Markdown("")
                    with gr.Accordion("Browse loaded part files", open=True):
                        folder_browser = gr.Markdown(
                            "Choose **Refresh GCS folder** to load your available files."
                        )

                with gr.Tab("Administration"):
                    with gr.Group(visible=False) as admin_panel:
                        gr.Markdown("### 1. Create a user")
                        new_username = gr.Textbox(label="Username")
                        with gr.Row():
                            new_user_password = gr.Textbox(
                                label="Temporary password", type="password"
                            )
                            new_user_password_confirm = gr.Textbox(
                                label="Confirm temporary password", type="password"
                            )
                        with gr.Row():
                            new_user_admin = gr.Checkbox(
                                label="Give administrator access", value=False
                            )
                            create_user_button = gr.Button(
                                "Create user", variant="primary"
                            )
                        create_user_status = gr.Markdown("")

                        gr.Markdown("### 2. Manage file access")
                        assignment_user = gr.Dropdown(
                            choices=admin_usernames(),
                            label="Select user",
                            filterable=True
                        )
                        assignment_files = gr.CheckboxGroup(
                            choices=GCS_FILES,
                            value=[],
                            label="Files this user can access",
                            elem_id="zion_file_access_checks"
                        )
                        save_access_button = gr.Button(
                            "Save file access", variant="primary"
                        )
                        assignment_status = gr.Markdown("")
                        with gr.Accordion("Current assignments", open=True):
                            assignment_list = gr.Markdown(assignment_summary())

                        gr.Markdown("### 3. Built files viewer")
                        gr.Markdown(
                            f"Read-only files saved in `gs://{BUCKET_NAME}/{BUILD_FOLDER}`."
                        )
                        built_file_dropdown = gr.Dropdown(
                            choices=[],
                            label="Built file",
                            filterable=True
                        )
                        with gr.Row(elem_classes=["zion-built-toolbar"]):
                            built_jump_line = gr.Number(
                                value=1,
                                precision=0,
                                label="Jump to row",
                                scale=3
                            )
                            built_go_button = gr.Button("Go", variant="primary", scale=1)
                            built_previous_button = gr.Button("Previous", scale=1)
                            built_next_button = gr.Button("Next", scale=1)
                            refresh_built_files_button = gr.Button(
                                "Refresh files", variant="secondary", scale=1
                            )
                        built_file_status = gr.Markdown("")
                        built_file_viewer = gr.HTML(
                            empty_editor_html("Select a built file to view."),
                            elem_id="zion_built_file_viewer"
                        )
                        built_file_total = gr.Number(value=0, visible=False)

    notes_fab = gr.Button(
        "📝",
        variant="primary",
        elem_id="zion_notes_fab"
    )

    with gr.Group(visible=False, elem_id="zion_notes_panel") as notes_panel:
        with gr.Row(elem_classes=["zion-notes-heading"]):
            gr.Markdown("### Sticky notes")
            notes_close_button = gr.Button(
                "×",
                elem_id="zion_notes_close",
                scale=0
            )
        note_context_status = gr.Markdown(
            "Your personal global notepad"
        )
        shared_admin_notes = gr.HTML("")
        note_text = gr.Textbox(
            label="My sticky note",
            placeholder="Write a reminder, editing note, or anything you want to remember...",
            lines=7,
            max_lines=14,
            elem_id="zion_note_text"
        )
        with gr.Row(elem_classes=["zion-note-actions"]):
            copy_note_button = gr.Button(
                "⎘",
                variant="secondary",
                elem_id="zion_copy_note_button",
                scale=0
            )
            save_note_button = gr.Button(
                "Save note",
                variant="primary",
                scale=1
            )
        note_save_status = gr.Markdown("")


    # ========================================================
    # FILE SELECTOR
    # ========================================================

    file_dropdown = gr.Dropdown(

        choices=[],

        value=None,

        label="GCS file",

        elem_id=
            "zion_file_selector"

    )


    # ========================================================
    # NAVIGATION
    # ========================================================

    with gr.Row(elem_classes=["zion-toolbar"]):

        jump_line = gr.Number(

            value=1,

            precision=0,

            label="Jump to row",

            scale=2

        )


        jump_button = gr.Button(

            "↵",

            variant="primary",

            scale=1,

            elem_id="zion_go_button"

        )


        previous_button = gr.Button(

            "‹",

            scale=1,

            elem_id="zion_previous_button"

        )


        next_button = gr.Button(

            "›",

            scale=1,

            elem_id="zion_next_button"

        )


        refresh_button = gr.Button(

            "↻",

            scale=1,

            elem_id="zion_refresh_button"

        )

        fullscreen_button = gr.Button(

            "⛶",

            variant="secondary",

            scale=1,

            elem_id="zion_fullscreen_button"

        )

        save_button = gr.Button(

            "✓",

            variant="primary",

            scale=1,

            elem_id="zion_save_button"

        )

        build_button = gr.Button(

            "▣",

            variant="secondary",

            scale=1,

            elem_id="zion_build_button"

        )

        help_button = gr.Button(

            "?",

            variant="secondary",

            scale=1,

            elem_id="zion_help_button"

        )

        account_button = gr.Button(
            "☰",
            variant="secondary",
            scale=1,
            elem_id="zion_account_button"
        )

    # ========================================================
    # STATUS
    # ========================================================

    status = gr.Markdown(
        "Loading..."
    )

    save_status = gr.Markdown(
        "",
        elem_id="zion_save_status"
    )


    # ========================================================
    # EDITOR
    # ========================================================

    editor = gr.HTML(

        empty_editor_html(
            "Loading file..."
        ),

        elem_id=
            "zion_editor_container"

    )

    with gr.Row(elem_classes=["zion-bottom-nav"]):
        bottom_previous_button = gr.Button(
            "‹",
            variant="secondary",
            elem_id="zion_bottom_previous_button",
            scale=1
        )
        bottom_fullscreen_button = gr.Button(
            "⛶",
            variant="secondary",
            elem_id="zion_bottom_fullscreen_button",
            scale=1
        )
        bottom_next_button = gr.Button(
            "›",
            variant="primary",
            elem_id="zion_bottom_next_button",
            scale=1
        )


    # ========================================================
    # SAVE / BUILD
    # ========================================================

    with gr.Row(visible=False):

      legacy_fullscreen_button = gr.Button(
          "⛶ FULL SCREEN EDITOR",
          variant="secondary",
          scale=2,
          elem_id="zion_legacy_fullscreen_button"
      )

      legacy_save_button = gr.Button(
          "✓ SAVE ALL CHANGES",
          variant="primary",
          scale=2
      )

      legacy_build_button = gr.Button(
          "🏗 BUILD FINAL TEXT FILE",
          variant="secondary",
          scale=2
      )

      legacy_save_status = gr.Markdown("")

    # ========================================================
    # HELP
    # ========================================================

    gr.Markdown(
        """
### Editor controls

- **Click TEXT** → edit directly
- **Paste 1 line** → replace selected row
- **Paste 2 lines** → replace selected row + exactly 1 new row
- **Paste N lines** → replace selected row + exactly N−1 new rows
- **Trailing clipboard newlines** → ignored
- **Empty clipboard lines** → ignored (not inserted as rows)
- **Word count** → updates automatically
- **✎** → unsaved edited row
- **✓** → saved
- **+** → newly inserted row
- **↑** → insert above
- **↓** → insert below
- **×** → delete
- **GO** → jump to logical row
- **NEXT / PREVIOUS** → navigate
- **SAVE ALL CHANGES** → upload sparse JSON patch to GCS
- **BUILD FINAL TEXT FILE** → create complete edited TXT file

### Storage behavior

The original GCS dataset is never overwritten.

**SAVE ALL CHANGES** stores sparse edits as a JSON patch:

`gs://zion_model/dataset/editor_patches_v4/`

**BUILD FINAL TEXT FILE** creates the complete edited dataset:

`gs://zion_model/dataset/editor_built_v4/`

Repeated BUILD operations update the same output file.
"""
        ,
        visible=False
    )


    # ========================================================
    # EVENT CHANNEL
    # ========================================================

    event_payload = gr.Textbox(

        value="",

        label="",

        elem_id=
            "zion_event_payload",

        show_label=False,

        interactive=True

    )

    # Browser actions are sent one at a time.  This visible-but-offscreen
    # acknowledgement component lets JavaScript know that Gradio finished the
    # preceding callback before it dispatches the next edit/insert/delete.
    event_ack = gr.Textbox(
        value="",
        label="",
        elem_id="zion_event_ack",
        show_label=False,
        interactive=False
    )

    # Programmatic Textbox ``input`` events are not consistently forwarded by
    # every Gradio 5 frontend.  JavaScript updates the payload, then clicks
    # this real Gradio button to invoke the Python mutation callback.
    event_submit = gr.Button(
        "Dispatch editor action",
        elem_id="zion_event_submit"
    )


    # ========================================================
    # STRUCTURAL REFRESH COUNTER
    # ========================================================

    refresh_counter = gr.Number(

        value=0,

        visible=False

    )


    current_total = gr.Number(

        value=0,

        visible=False

    )


    # ========================================================
    # FILE CHANGE
    # ========================================================

    change_password_button.click(
        change_own_password,
        inputs=[current_password, new_password, confirm_password],
        outputs=[password_status],
        show_progress="minimal"
    )

    create_user_button.click(
        create_user,
        inputs=[
            new_username,
            new_user_password,
            new_user_password_confirm,
            new_user_admin
        ],
        outputs=[
            create_user_status,
            assignment_user,
            assignment_files,
            assignment_list
        ],
        show_progress="minimal"
    )

    assignment_user.change(
        load_user_file_access,
        inputs=[assignment_user],
        outputs=[assignment_files, assignment_status],
        show_progress="minimal"
    )

    upload_dataset_button.click(
        upload_dataset_file,
        inputs=[dataset_upload, file_dropdown, assignment_user],
        outputs=[
            upload_dataset_status,
            file_dropdown,
            assignment_files,
            folder_browser
        ],
        show_progress="full"
    )

    refresh_folder_button.click(
        browse_dataset_folder,
        inputs=[file_dropdown, assignment_user],
        outputs=[
            file_dropdown,
            assignment_files,
            folder_browser,
            upload_dataset_status
        ],
        show_progress="minimal"
    )

    save_access_button.click(
        save_user_file_access,
        inputs=[assignment_user, assignment_files],
        outputs=[assignment_status, assignment_list],
        show_progress="minimal"
    )

    save_note_button.click(
        save_global_note,
        inputs=[note_text],
        outputs=[note_text, shared_admin_notes, note_save_status],
        show_progress="minimal"
    )

    notes_fab.click(
        open_global_notes_panel,
        inputs=[],
        outputs=[
            notes_panel,
            note_text,
            shared_admin_notes,
            note_context_status,
            note_save_status
        ],
        show_progress="minimal"
    )

    notes_close_button.click(
        close_global_notes_panel,
        inputs=[],
        outputs=[notes_panel],
        show_progress="hidden"
    )

    refresh_built_files_button.click(
        refresh_built_file_viewer,
        inputs=[],
        outputs=[built_file_dropdown, built_file_status],
        show_progress="minimal"
    )

    built_file_dropdown.change(
        load_built_file_page,
        inputs=[built_file_dropdown, built_jump_line],
        outputs=[
            built_file_viewer,
            built_file_status,
            built_jump_line,
            built_file_total
        ],
        show_progress="minimal"
    )

    built_go_button.click(
        load_built_file_page,
        inputs=[built_file_dropdown, built_jump_line],
        outputs=[built_file_viewer, built_file_status, built_jump_line, built_file_total],
        show_progress="minimal"
    )

    built_previous_button.click(
        previous_built_file_page,
        inputs=[built_file_dropdown, built_jump_line],
        outputs=[built_file_viewer, built_file_status, built_jump_line, built_file_total],
        show_progress="minimal"
    )

    built_next_button.click(
        next_built_file_page,
        inputs=[built_file_dropdown, built_jump_line],
        outputs=[built_file_viewer, built_file_status, built_jump_line, built_file_total],
        show_progress="minimal"
    )

    file_dropdown.change(

        select_file,

        inputs=[
            file_dropdown
        ],

        outputs=[
            editor,
            status,
            jump_line,
            current_total
        ],

        show_progress="minimal"

    )


    # ========================================================
    # INITIAL LOAD
    # ========================================================

    # Register the controls through a load event as well as the app-level JS
    # setting.  Gradio releases have moved custom JS between Blocks and
    # launch(); this idempotent fallback guarantees that the row controls are
    # initialized whichever lifecycle path the installed version uses.
    app.load(
        fn=None,
        js=JS
    )

    app.load(

        load_user_workspace,

        inputs=[],

        outputs=[
            file_dropdown,
            editor,
            status,
            jump_line,
            current_total,
            account_status,
            admin_panel,
            assignment_user,
            assignment_files,
            assignment_status,
            assignment_list,
            built_file_dropdown
        ],

        show_progress="minimal"

    ).then(
        load_global_notes,
        inputs=[],
        outputs=[
            note_text,
            shared_admin_notes,
            note_context_status,
            note_save_status
        ],
        show_progress="minimal"
    )


    # ========================================================
    # GO
    # ========================================================

    jump_button.click(

        refresh_current,

        inputs=[
            file_dropdown,
            jump_line
        ],

        outputs=[
            editor,
            status,
            jump_line,
            current_total
        ],

        show_progress="minimal"

    )


    # ========================================================
    # NEXT
    # ========================================================

    next_button.click(

        next_page,

        inputs=[
            file_dropdown,
            jump_line
        ],

        outputs=[
            editor,
            status,
            jump_line,
            current_total
        ],

        show_progress="minimal"

    )

    bottom_next_button.click(
        next_page,
        inputs=[file_dropdown, jump_line],
        outputs=[editor, status, jump_line, current_total],
        show_progress="minimal"
    )


    # ========================================================
    # PREVIOUS
    # ========================================================

    previous_button.click(

        previous_page,

        inputs=[
            file_dropdown,
            jump_line
        ],

        outputs=[
            editor,
            status,
            jump_line,
            current_total
        ],

        show_progress="minimal"

    )

    bottom_previous_button.click(
        previous_page,
        inputs=[file_dropdown, jump_line],
        outputs=[editor, status, jump_line, current_total],
        show_progress="minimal"
    )


    # ========================================================
    # REFRESH
    # ========================================================

    refresh_button.click(

        refresh_current,

        inputs=[
            file_dropdown,
            jump_line
        ],

        outputs=[
            editor,
            status,
            jump_line,
            current_total
        ],

        show_progress="minimal"

    )


    # ========================================================
    # BROWSER EVENTS
    # ========================================================

    event_submit.click(

        process_editor_event,

        inputs=[
            event_payload,
            file_dropdown,
            jump_line
        ],

        outputs=[
            event_ack,
            save_status,
            refresh_counter,
            editor,
            status,
            jump_line,
            current_total
        ],

        show_progress="minimal"

    )


    # ========================================================
    # STRUCTURAL REFRESH
    # ========================================================

    # Structural events refresh the editor directly in
    # ``process_editor_event``. A hidden-counter change callback was racy in
    # some Gradio 5 versions and made insert/delete/paste appear to do nothing.
    # The event input is deliberately not also an output: clearing it from an
    # older response could overwrite and lose a newer browser action.


    # ========================================================
    # SAVE
    # ========================================================

    save_button.click(

        save_all_changes,

        inputs=[
            file_dropdown
        ],

        outputs=[
            save_status
        ],

        show_progress="minimal"

    ).then(

        refresh_current,

        inputs=[
            file_dropdown,
            jump_line
        ],

        outputs=[
            editor,
            status,
            jump_line,
            current_total
        ],

        show_progress="minimal"

    )


    # ========================================================
    # BUILD FINAL TEXT FILE
    # ========================================================

    build_button.click(

        build_final_text_file,

        inputs=[
            file_dropdown
        ],

        outputs=[
            save_status
        ],

        show_progress="full"

    )


# ============================================================
# 44. LAUNCH
# ============================================================

print()
print("=" * 80)
print("ZION SMART GCS LINE EDITOR V4.4.1 READY")
print("=" * 80)

print()

print(
    f"GCS source: "
    f"gs://{BUCKET_NAME}/{GCS_FOLDER}"
)

print(
    f"Patch folder: "
    f"gs://{BUCKET_NAME}/{PATCH_FOLDER}"
)

print(
    f"Built files: "
    f"gs://{BUCKET_NAME}/{BUILD_FOLDER}"
)

print(
    f"Local DB: "
    f"{DB_FILE}"
)

print(
    f"Page size: "
    f"{PAGE_SIZE}"
)

print()

print(
    "Original GCS files are NEVER rewritten."
)

print(
    "Edits are stored as sparse SQLite changes."
)

print(
    "Single-line paste replaces the selected row."
)

print(
    "Two-line paste = replace 1 + add exactly 1."
)

print(
    "N-line paste = replace 1 + add exactly N-1."
)

print(
    "Trailing clipboard empty lines are ignored."
)

print(
    "Empty clipboard lines are ignored."
)

print(
    "Word counts update automatically."
)

print(
    "Status updates automatically."
)

print(
    "Insert ABOVE works."
)

print(
    "Insert BELOW works."
)

print(
    "Repeated INSERT works."
)

print(
    "DELETE works."
)

print(
    "Repeated DELETE works."
)

print(
    "SAVE uploads JSON patch."
)

print(
    "BUILD creates complete final text file."
)

print(
    "BUILD updates the same output file."
)

print()

print("=" * 80)


# The legacy editor core keeps document metadata in one in-process state
# object. Serialize callbacks so two signed-in users cannot switch that state
# underneath one another while an edit is being applied.
app.queue(default_concurrency_limit=1)

app.launch(

    share=False,

    server_name="127.0.0.1",

    server_port=7860,

    auth=authenticate,

    auth_message=(
        "Sign in to the ZION editor. Contact the administrator if no file "
        "has been assigned to your account."
    ),

    show_error=True,

    debug=False,

    **LAUNCH_STYLE_KWARGS,

    **LAUNCH_FOOTER_KWARGS

)
