import re

PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.arw'}
VIDEO_EXTENSIONS = {'.mov', '.mp4'}
SIDECAR_EXTENSIONS = {'.xmp'}
ALL_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS | SIDECAR_EXTENSIONS

SKIP_NAMES = {'.DS_Store', 'Icon\r', '.Trashes', '.Spotlight-V100',
              '.fseventsd', 'Thumbs.db', '.DocumentRevisions-V100'}

DEFAULT_SOURCE = '/Volumes/Extreme SSD'
DEFAULT_DEST = '/Volumes/fileshares/photosCleanup2026'

# Filename date patterns (order matters - most specific first)
FILENAME_PATTERNS = [
    # "2013-01-18 15.21.17" or with burst suffix "-1"
    re.compile(r'(\d{4})-(\d{2})-(\d{2})\s+\d{2}\.\d{2}\.\d{2}'),
    # "2000-01-07-02-15-21"
    re.compile(r'(\d{4})-(\d{2})-(\d{2})-\d{2}-\d{2}-\d{2}'),
    # "20170520-" compact date prefix
    re.compile(r'(\d{4})(\d{2})(\d{2})-'),
    # "2022-10-01" anywhere
    re.compile(r'(\d{4})-(\d{2})-(\d{2})'),
]
