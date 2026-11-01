import hashlib


def sha256_file(filepath):
    """Compute SHA256 hash of a file using chunked reading."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(1_048_576)  # 1 MB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
