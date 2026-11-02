import re

RAW_EXTENSIONS = {'.arw', '.dng', '.cr2', '.cr3', '.nef', '.raf', '.orf', '.rw2', '.pef', '.srw'}
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.gif', '.tif', '.tiff',
                    '.psd', '.webp'} | RAW_EXTENSIONS
VIDEO_EXTENSIONS = {'.mov', '.mp4', '.m4v', '.avi', '.wmv', '.mts', '.m2ts', '.3gp', '.mpg', '.mpeg'}
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS
# Sidecars travel with the media file that shares their stem (IMG.xmp or IMG.ARW.xmp).
SIDECAR_EXTENSIONS = {'.xmp', '.aae', '.thm'}
ALL_EXTENSIONS = MEDIA_EXTENSIONS | SIDECAR_EXTENSIONS

# Junk files that may be deleted when they are the only thing left in a directory.
JUNK_NAMES = {'.DS_Store', 'Thumbs.db', 'Icon\r', 'desktop.ini', '.dropbox.device', '.dropbox'}
# NAS thumbnail caches (QNAP / Synology): regenerable, so a folder holding only these is empty.
THUMB_CACHE_DIRS = {'.@__thumb', '@eaDir'}

SKIP_NAMES = JUNK_NAMES | {
    '.Trashes', '.Spotlight-V100', '.fseventsd', '.DocumentRevisions-V100', '.TemporaryItems',
    # NAS recycle bins, snapshots and thumbnail caches (QNAP / Synology).
    '@Recycle', '@Recently-Snapshot', '#recycle', '#snapshot', '@eaDir', '.@__thumb', '.streams',
}

# Application bundles whose contents must never be moved: doing so corrupts the catalog/library.
SKIP_DIR_SUFFIXES = ('.photoslibrary', '.cocatalog', '.cosessiondb', '.lrdata', '.lrcat-data',
                     '.aplibrary', '.migratedphotolibrary')

# Date sources trusted enough to file a photo; anything else is left in place for review.
TRUSTED_DATE_SOURCES = ('photos', 'exif', 'filename', 'dirname')   # 'photos': the Apple Photos library's date

# Filename date patterns (order matters - most specific first)
FILENAME_PATTERNS = [
    # "2013-01-18 15.21.17" or with burst suffix "-1"
    re.compile(r'(\d{4})-(\d{2})-(\d{2})\s+\d{2}\.\d{2}\.\d{2}'),
    # "2000-01-07-02-15-21"
    re.compile(r'(\d{4})-(\d{2})-(\d{2})-\d{2}-\d{2}-\d{2}'),
    # "20170520-140151", "IMG_20170520_140151", "PXL_20230101_123456789"
    re.compile(r'(?<!\d)(\d{4})(\d{2})(\d{2})[-_]\d{6}'),
    # "20170520-" compact date prefix
    re.compile(r'(?<!\d)(\d{4})(\d{2})(\d{2})-'),
    # "2022-10-01" anywhere
    re.compile(r'(\d{4})-(\d{2})-(\d{2})'),
]

# Backups are imported copy-only: nothing inside them is ever deleted (HFS Backups.backupdb,
# APFS snapshot mounts under /Volumes/.timemachine, and network backup bundles).
TIME_MACHINE_MARKERS = ('/Backups.backupdb/', '/.timemachine/', '.backupbundle/', '.sparsebundle/')


def is_time_machine_path(path):
    return any(m in path.rstrip('/') + '/' for m in TIME_MACHINE_MARKERS)


# Name of the catalog a previous run of this tool left at the root of its destination.
PRIOR_CATALOG_NAME = 'photo_organizer.db'
