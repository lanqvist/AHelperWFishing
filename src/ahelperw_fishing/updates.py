"""Optional check for a newer release on GitHub (no data is sent besides the request itself).

Runs in a background thread with a short timeout; any failure is silent. The user can turn
it off in the app.
"""

import json
import re
import threading
import urllib.request

from . import REPOSITORY, __version__

RELEASES_API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
RELEASES_PAGE = f'https://github.com/{REPOSITORY}/releases/latest'


def parse_version(text):
    """'v0.4.1' -> (0, 4, 1); pre-release suffixes are ignored. None if not a version."""
    match = re.match(r'^v?(\d+)\.(\d+)(?:\.(\d+))?', str(text or '').strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def is_newer(candidate, current=__version__):
    new, old = parse_version(candidate), parse_version(current)
    return bool(new and old and new > old)


def latest_release(timeout=5.0):
    """(tag, page url) of the latest published release, or None."""
    request = urllib.request.Request(
        RELEASES_API,
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': f'AHelperWFishing/{__version__}'},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode('utf-8'))
    if data.get('draft') or data.get('prerelease'):
        return None
    return data.get('tag_name'), data.get('html_url') or RELEASES_PAGE


def check_async(callback):
    """Call ``callback(tag, url)`` from a worker thread when a newer release exists."""

    def work():
        try:
            found = latest_release()
        except Exception:  # offline, rate limited, proxy... never bother the user
            return
        if found and is_newer(found[0]):
            callback(*found)

    thread = threading.Thread(target=work, name='update-check', daemon=True)
    thread.start()
    return thread
