# supernote-sync

A dependency-free Python CLI for one-way host-to-Supernote folder synchronisation over the local **Browse & Access** server. It discovers the tablet, uploads new or changed files, and converts EPUBs to PDFs with headless Calibre.

## Tablet setup

On the tablet, open the top drop-down toolbar, enable **Browse & Access**, and keep its confirmation dialog open. The service normally listens at port `8089`.

## Install and use

```bash
python -m pip install -e .

# Scan the local /24, or specify the LAN range explicitly.
supernote-sync discover --subnet 192.168.1.0/24

# Upload one file. The known tablet address can be used directly.
supernote-sync upload --host http://192.168.1.114:8089 book.pdf --directory /INBOX
```

Discovery does more than find an open TCP port: it fetches the root page and recognises the Supernote directory listing. It defaults to the host's likely `/24`; use `--subnet` on a differently sized LAN.

## Folder sync and headless Calibre

Copy and edit the example configuration:

```bash
cp supernote-sync.toml.example supernote-sync.toml
supernote-sync sync --dry-run  # discovers the tablet automatically
supernote-sync sync
```

The `[calibre].arguments` array is passed verbatim to `ebook-convert INPUT.epub OUTPUT.pdf`. The example uses the supplied fixed PDF profile: Kindle input; Generic e-ink HD output; 1920×2560 device pixels; Baskervville body serif; 25pt text; pixel margins; PDF page numbers and table of contents. The bundled Baskervville static fonts are exposed only to the Calibre subprocess through a temporary Fontconfig configuration—nothing is installed on the host. Replace `arguments` if you want a different profile.

Sync is intentionally conservative: it examines direct files in `source_directory`, converts each `.epub` to a cached PDF, and uploads only new or changed output using a local SHA-256 manifest. It never deletes or overwrites files on the tablet. The remote directory must already exist. `--dry-run` makes neither host nor tablet changes.

## Python API

```python
from supernote_sync import SupernoteConnection, discover

# Endpoint values are e.g. http://192.168.1.114:8089
endpoint = discover("192.168.1.0/24")[0]
client = SupernoteConnection.from_endpoint(endpoint)
assert client.probe()
client.upload_file("build/book.pdf", "/INBOX/Books")
```

`upload_file` uses the Browse & Access form field expected by the tablet (`file`) and streams data from disk, avoiding a second full-file memory copy. It intentionally supports only uploading at this stage.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

The test suite uses a local HTTP server and never contacts the tablet.
