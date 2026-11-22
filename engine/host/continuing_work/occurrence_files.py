"""Read, identify and atomically replace local continuing-work occurrences."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def read(path):
    return json.loads(Path(path).read_text())

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    tmp.replace(path)

def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', value):
        raise ValueError('Use a simple local identity: '+str(value))
    return value

def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label+' must be nonempty text')
    return value
