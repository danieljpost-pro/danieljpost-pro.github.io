#!/usr/bin/env python3
"""Build anonymized/resume.json from the real resume data.

Same structure as the source file, with every personal identifier swapped for a plausible
fake: the owner's name and contact details, recommenders' names, phones, emails and
LinkedIn URLs, employer and client names, URLs and logos, places, and any of these
appearing in free text. Dates, titles, skills and accomplishment wording are kept, so
resume parsers have realistic material to work on.

Fake names are deliberately plausible: Greenhouse documents placeholder-looking employer
names ("Company 1", "Any Company") as a cause of failed parses.

After writing, the script scans the output for every original identifier it knows about
and exits 1 if any survive.

    python3 tools/make_anonymized.py            # write anonymized/resume.json
    python3 tools/make_anonymized.py --check    # leak-check the existing file only
"""

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "resume-2026.11-develop" / "resume.json"
OUT = ROOT / "anonymized" / "resume.json"
ANON_NS = uuid.UUID("6f3e2b1a-9c4d-5e8f-a7b6-0d1c2e3f4a5b")

PERSON = {
    "name": "Jordan R. Ellison",
    "short": "Jordan R.",
    "given": "Jordan",
    "email": "jordan.ellison@example.com",
    "birthDate": "1980-01-01",
    "home": "https://www.example.com/jordan-ellison",
}

# Real company full name -> (fake full name, extra aliases that appear in free text).
COMPANIES = {
    "Daniel J. Post Consulting, Inc": ("Ellison Technical Consulting, Inc", ["Daniel J. Post Consulting, Inc.", "Daniel J. Post Consulting"]),
    "ThreatLVL": ("Brightwater Analytics, Inc.", []),
    "Mindora AI": ("Corvana Health Systems, LLC", ["Mindora"]),
    "Imagination Guild": ("Larkspur Talent Partners, LLC", []),
    "United Natural Foods, Inc": ("Prairie Provisions Distribution, Inc.", ["United Natural Foods", "UNFI"]),
    "Vivial Media, LLC": ("Kestrel Local Media, LLC", ["Vivial Media", "Vivial"]),
    "Resorts and Lodges": ("Timberline Stays Group, Inc.", []),
    "Blue Earth Interactive / Torrch": ("Copperleaf Interactive, LLC", ["Blue Earth Interactive", "Torrch"]),
    "FedEx": ("Northgate Parcel Services, Inc.", []),
    "BrokerBin": ("Partsline Exchange, Inc.", []),
    "Homes for Heroes": ("Hometown Service Realty Network", []),
    "Starkey": ("Clearsound Hearing Technologies", []),
    "Irish Titan": ("Ironbridge Digital Agency", []),
    "ezXchanges": ("Tradeweave Exchanges, Inc.", []),
    "Build Technology Group": ("Fieldstone Technology Group", []),
    "Robert Half Technology": ("Summit Staffing Technology", ["Robert Half"]),
    "Horizontal": ("Meridian Digital Partners", []),
    "bluedoor publishing": ("Bluefinch Publishing", []),
    "bluedoor labs": ("Bluefinch Labs", []),
    "Star Asia": ("Pacific Crest Trading Co.", ["Star Asia International"]),
    "Go Buy Local / Do Good Tech": ("ShopNear Civic Technology", []),
    "The Lacek Group": ("Halvorsen Loyalty Group", ["Lacek"]),
    "Target Corporation": ("Northfield Retail Corporation", ["Target.com", "Target"]),
    "RAZR Marketing": ("Brightline Marketing", ["RAZR"]),
    "Twin Cities Public Television": ("Riverbend Public Media", ["TPT"]),
    "U.S. Bank": ("Heartland Federal Bank", ["US Bank"]),
    "Life Time Fitness": ("Everstride Fitness Clubs", ["Life Time", "Lifetime Fitness"]),
    "Mayo Clinic": ("Wellhaven Medical Center", ["mayoclinic.org", "Mayo"]),
    "BlueStem Brands": ("Tallgrass Retail Brands", ["Bluestem Brands", "BlueStem"]),
    "Network Instruments": ("Signalpath Instruments", []),
    "TDX Tech": ("Keystone Data Tech", []),
    "MN Center for Energy and the Environment": ("Center for Regional Energy Efficiency", []),
    "Visible Customer": ("ClearView Customer Analytics", []),
    "Search Leaders": ("Pinnacle Search Partners", []),
}

# Other identifying phrases found in the free text (recommender titles, project names).
EXTRA = [
    ("Inter Atlanta FC", "Riverside United FC"),
    ("Kettle River Consulting", "Stonebrook Consulting"),
    ("Stratasys", "Layerworks Manufacturing"),
    ("Help Teachers Help Students", "Help Schools Help Students"),
    ("Slingshot", "Catapult"),
]

PLACES = [
    ("Greater Twin Cities Area", "Greater Columbus Area"),
    ("Twin Cities", "Columbus"),
    ("Minneapolis", "Columbus"),
    ("Saint Paul", "Dublin"),
    ("St. Paul", "Dublin"),
    ("MN", "OH"),
]

# Plausible names for recommenders, assigned in order of appearance.
PEOPLE = [
    "Morgan Hale", "Riley Sandoval", "Casey Lindqvist", "Avery Okafor", "Quinn Albrecht",
    "Taylor Brannigan", "Jamie Castellanos", "Drew Halvorsen", "Peyton Marlowe", "Reese Abernathy",
    "Skyler Dunmore", "Rowan Takahashi", "Emerson Vance", "Finley Oduya", "Hayden Prescott",
]

FALLBACK_COMPANY_WORDS = (
    ["Halcyon", "Northbeam", "Granite", "Lakeshore", "Sterling", "Cedar", "Bluepeak", "Ironwood"],
    ["Systems", "Solutions", "Partners", "Labs", "Group", "Technologies", "Works", "Digital"],
)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def fallback_company(fullname: str) -> str:
    h = int(hashlib.sha256(fullname.encode()).hexdigest(), 16)
    a, b = FALLBACK_COMPANY_WORDS
    return f"{a[h % len(a)]} {b[(h // len(a)) % len(b)]}, Inc."


# Names that are also ordinary words: matched case-sensitively ("Target" the company, not "a compilation target").
ORDINARY_WORDS = {"target", "horizontal", "lifetime", "life time", "starkey"}
# Not identifiers: placeholder hosts already present in the source data.
NOT_IDENTIFYING = {"example.com", "www.example.com"}


def literal_pattern(term: str) -> re.Pattern:
    """Match a literal term with word boundaries where it starts/ends with a word character.

    Terms of 5+ characters match case-insensitively, unless they are ordinary words."""
    left = r"(?<![\w])" if term[:1].isalnum() else ""
    right = r"(?![\w])" if term[-1:].isalnum() else ""
    flags = re.I if len(term) >= 5 and term.lower() not in ORDINARY_WORDS else 0
    return re.compile(left + re.escape(term) + right, flags)


def build_rules(data: dict) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (replacement rules, original identifiers for the leak check)."""
    rules: list[tuple[str, str]] = []
    originals: list[str] = []

    me = data.get("me", {})
    real_name = me.get("name", "Daniel J. Post")
    given = me.get("givenName", real_name.split()[0])
    short = " ".join(real_name.split()[:2])            # "Daniel J."
    rules += [
        (real_name, PERSON["name"]),
        (short + "’s", PERSON["short"] + "’s"),
        (short + "'s", PERSON["short"] + "'s"),
        (short, PERSON["short"]),
        (given + "’s", PERSON["given"] + "’s"),
        (given + "'s", PERSON["given"] + "'s"),
        (given, PERSON["given"]),
    ]
    originals += [real_name, given]

    for c in data.get("companies", []):
        full = c.get("fullname") or c.get("name")
        fake, aliases = COMPANIES.get(full, (None, []))
        if fake is None:
            fake = fallback_company(full)
            print(f"warning: no fake name mapped for company {full!r}; using {fake!r}. Add it to COMPANIES.", file=sys.stderr)
        rules.append((full, fake))
        rules += [(a, fake) for a in aliases]
        originals += [full, *aliases]
        if c.get("url"):
            host = re.sub(r"^https?://(www\.)?", "", c["url"]).split("/")[0]
            originals.append(host)

    for i, r in enumerate(data.get("recommendations", [])):
        fake = PEOPLE[i % len(PEOPLE)]
        parts = r.get("name", "").split()
        if not parts:
            continue
        rules.append((r["name"], fake))
        first = " ".join(parts[:-1]) or parts[0]      # "Cevat Kerim" stays together
        fake_first = fake.split()[0]
        rules += [(first + "’s", fake_first + "’s"), (first + "'s", fake_first + "'s"), (first, fake_first)]
        originals += [r["name"], first]
        for key in ("email", "phone", "linkedIn"):
            if r.get(key):
                originals.append(r[key])

    rules += EXTRA + PLACES
    originals += [a for a, _ in EXTRA] + [a for a, _ in PLACES if len(a) > 2]
    originals += ["danieljpost", me.get("email", ""), real_name.split()[-1] + " Consulting"]

    # Longest first, so "Daniel J. Post" is replaced before "Daniel".
    rules.sort(key=lambda r: len(r[0]), reverse=True)
    return rules, [o for o in originals if o and o.lower() not in NOT_IDENTIFYING]


def apply(rules, value):
    if isinstance(value, str):
        for real, fake in rules:
            value = literal_pattern(real).sub(fake, value)
        value = re.sub(r"https?://(?:www\.)?danieljpost\.pro", PERSON["home"], value)
        value = re.sub(r"https?://raw\.githubusercontent\.com/danieljpost/[\w.-]+", PERSON["home"] + "/data", value)
        return value
    if isinstance(value, list):
        return [apply(rules, v) for v in value]
    if isinstance(value, dict):
        return {k: apply(rules, v) for k, v in value.items()}
    return value


def anonymize(data: dict) -> dict:
    rules, _ = build_rules(data)
    out = apply(rules, json.loads(json.dumps(data)))

    me = out.setdefault("me", {})
    me.update({"name": PERSON["name"], "givenName": PERSON["given"], "email": PERSON["email"], "birthDate": PERSON["birthDate"]})
    if "image" in me:
        me["image"] = ""
    for key in ("$id",):
        if key in out:
            out[key] = PERSON["home"] + "/resume.json"

    # Companies: fake URLs, no logos, fresh ids (the originals are uuid5 of the real URL).
    company_ids = {}
    for c in out.get("companies", []):
        name_slug = slug(c.get("fullname") or c.get("name") or "company")
        c["name"] = name_slug
        c["url"] = f"https://www.example.com/{name_slug}"
        if "logo" in c:
            c["logo"] = ""
        new_id = str(uuid.uuid5(ANON_NS, c["url"]))
        company_ids[c["_id"]] = new_id
        c["_id"] = new_id

    # Gigs: opaque ids (several real ids are company names, e.g. "target").
    gig_ids = {}
    for i, g in enumerate(out.get("gigs", []), 1):
        gig_ids[g["_id"]] = f"gig-{i:03d}"
        g["_id"] = gig_ids[g["_id"]]
        for key in ("companyId", "contractorId", "recruiterId"):
            if g.get(key):
                g[key] = company_ids.get(g[key], g[key])

    for i, r in enumerate(out.get("recommendations", []), 1):
        fake = PEOPLE[(i - 1) % len(PEOPLE)]
        r["_id"] = f"rec-{i:03d}"
        r["gigId"] = gig_ids.get(r.get("gigId"), f"gig-unknown-{i:03d}")
        if r.get("company"):
            r["company"] = company_ids.get(r["company"], r["company"])
        if "email" in r:
            r["email"] = f"{slug(fake).replace('-', '.')}@example.com"
        if "phone" in r:
            r["phone"] = f"555-01{i:02d}"
        if "linkedIn" in r:
            r["linkedIn"] = f"https://www.example.com/in/{slug(fake)}"
    return out


def leak_check(original: dict, text: str) -> list[str]:
    _, originals = build_rules(original)
    hits = []
    for term in sorted(set(originals), key=len, reverse=True):
        for m in literal_pattern(term).finditer(text):
            hits.append(f"{term!r}: …{text[max(0, m.start() - 40):m.end() + 40]}…")
            break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only leak-check the existing anonymized file")
    args = ap.parse_args()

    original = json.loads(SRC.read_text(encoding="utf-8"))
    if args.check:
        text = OUT.read_text(encoding="utf-8")
    else:
        anon = anonymize(original)
        text = json.dumps(anon, indent=2, ensure_ascii=False) + "\n"
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)} from {SRC.relative_to(ROOT)}")

    hits = leak_check(original, text)
    if hits:
        print(f"LEAK CHECK FAILED: {len(hits)} original identifier(s) survive:", file=sys.stderr)
        for h in hits:
            print("  " + h, file=sys.stderr)
        return 1
    print("leak check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
