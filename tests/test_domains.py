"""Every mail address and URL in the repository is invented or public reference.

Allowed hosts: anything under the reserved .example and .test names; the
public reference sites the app or the docs link to (GitHub, UNECE, OurAirports,
w3.org, the MAPI namespace at schemas.microsoft.com, and the carrier and airline tracking sites of shipments.CARRIER_LINKS
and AWB_LINKS); a short fixed list of invented demo domains with real country
endings, for the tests that need a country read from the domain (none of them
resolved in DNS when chosen, 2026-09-28); and the two free-mail providers the
app ignores, only with an obvious placeholder in front (someone@gmail.com).
Needs no forbidden list, so it runs on CI too.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

from opplist import paths, shipments

RESERVED = (".example", ".test")
#: schemas.microsoft.com: the MAPI property-tag namespace Outlook's PropertyAccessor
#: reads (a fixed identifier like the w3.org SVG namespace, not a link).
#: schemas.openxmlformats.org: the namespace inside a .docx file, the same kind
#: of fixed identifier. anthropic.com / claude.com: the API Read quote was
#: written against and the page its prices were copied from; this edition
#: never calls it (tests/test_offline.py).
PUBLIC = {"github.com", "sil.org", "unece.org", "ourairports.com", "w3.org", "schemas.microsoft.com",
          "schemas.openxmlformats.org", "api.anthropic.com", "platform.claude.com"}
#: Invented, with real country endings (country_for_tld); none resolves.
DEMO_CCTLD = {"andes-demo.cl", "desert-demo.com.sa", "rio-demo.com.br", "inca-demo.com.pe",
              "azteca-demo.com.mx", "caribe-demo.com.co", "plata-demo.com.uy", "pampa-demo.com.ar"}
FREE_MAIL = {"gmail.com", "qq.com"}
PLACEHOLDER_LOCAL = {"someone", "somebody", "user", "nobody", "anyone"}
SKIP = {"opplist/web/static/worldmap.json", "opplist/web/static/usmap.json", "opplist/web/static/americas.json",
        # third-party code shipped as published: its licence headers name its authors and their sites
        # (leakcheck.THIRD_PARTY pins the file's bytes, tests/test_site.py checks the pin)
        "site/vendor/jspdf-4.2.1.umd.min.js"}
TEXT = {".py", ".json", ".txt", ".csv", ".md", ".html", ".sql", ".js", ".css", ".cmd", ".sh", ".yml", ".toml", ""}

EMAIL = re.compile(r"(?<![A-Za-z0-9._%+'-])([A-Za-z0-9._%+'-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
URL = re.compile(r"\b(?:https?|ftp)://([^/\s\"'<>)\]]+)|\bwww\.([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def tracking_hosts() -> set:
    urls = [url for _, _, url in shipments.CARRIER_LINKS] + [url for _, url in shipments.AWB_LINKS.values()]
    return {urlsplit(url).hostname for url in urls}


def _under(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def host_ok(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host.endswith(RESERVED) or host in ("localhost", "127.0.0.1"):
        return True
    return _under(host, PUBLIC | tracking_hosts() | DEMO_CCTLD)


def address_ok(local: str, host: str) -> bool:
    host = host.lower()
    if host in FREE_MAIL:
        return local.lower() in PLACEHOLDER_LOCAL
    return host_ok(host)


def tracked_files() -> list:
    root = paths.PROJECT_DIR
    try:
        # what a commit would contain: tracked plus untracked files .gitignore keeps
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
                             capture_output=True, check=True).stdout.decode()
        names = [n for n in out.split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file()
                 and ".git" not in p.parts and "__pycache__" not in p.parts]
    return [n for n in names if n not in SKIP and Path(n).suffix in TEXT and (root / n).is_file()]


def offenders() -> list:
    out = []
    for name in tracked_files():
        text = (paths.PROJECT_DIR / name).read_bytes().decode("utf-8", errors="replace")
        if "%40" in text or "%3A" in text or "%2F" in text:
            text += "\n" + unquote_plus(text)
        for no, line in enumerate(text.splitlines(), 1):
            for m in EMAIL.finditer(line):
                if not address_ok(m.group(1), m.group(2)):
                    out.append("{}:{}: {}".format(name, no, m.group(0)))
            for m in URL.finditer(line):
                host = (m.group(1) or ("www." + m.group(2)) or "").split("@")[-1].split(":")[0]
                if host and not host_ok(host):
                    out.append("{}:{}: {}".format(name, no, m.group(0)))
    return out


def test_every_address_and_url_is_invented_or_public():
    found = offenders()
    assert not found, "{} address(es) / URL(s) outside the allowlist:\n{}".format(len(found), "\n".join(found[:60]))


def test_the_rules():
    assert address_ok("ana", "tolvex.example") and address_ok("ops", "mail.boxlane.test")
    assert address_ok("someone", "gmail.com") and not address_ok("tideservice.demo70", "gmail.com")
    assert address_ok("omar", "desert-demo.com.sa") and not address_ok("omar", "zenithcargo.com.sa")
    assert host_ok("www.zim.com") and host_ok("github.com") and not host_ok("x.com")
    assert not host_ok("secure-web.cisco.com")
