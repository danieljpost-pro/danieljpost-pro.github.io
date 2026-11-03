# Anonymized resume data

`resume.json` in this folder is a copy of the real resume data
(`resume-2026.11-develop/resume.json`) with every personal identifier replaced by a plausible
fake. It exists so the resume page can be tested on **third-party resume-parser sites**
without sending them real personal details.

**Don't edit `resume.json` by hand.** Regenerate it after the real data changes:

```bash
python3 tools/make_anonymized.py            # rebuild anonymized/resume.json
python3 tools/make_anonymized.py --check    # leak-check the current file only
```

## What's replaced, and what isn't

| Replaced with plausible fakes | Kept as-is |
|---|---|
| Owner's name, email, birth date (photo URL blanked) | All dates |
| Recommenders' names, emails, phones (`555-01xx`), LinkedIn URLs | Job titles, gig types, relevance |
| Every employer, client, contractor and recruiter: names, URLs, logos, ids | Skills, categories, tiers |
| Places (Twin Cities → Columbus, OH) | Accomplishment and recommendation wording, apart from names |
| Gig ids (several real ones are company names, e.g. `target`) | The JSON structure |

Fake names are **deliberately realistic** ("Prairie Provisions Distribution, Inc.", not "Company 1").
Greenhouse documents placeholder-looking employer names as a cause of failed parses, and a parse
that fails for that reason tells us nothing about the page layout. Contact details use the
reserved `example.com` domain and the fictional `555-01xx` phone range, so they never reach a
real person.

**Limits:** this protects contact details and names from parser sites; it does not make the file
unlinkable to the real resume. The accomplishment and recommendation wording is unchanged, and the
real resume is public anyway.

**Leak check:** after writing, the script searches the output for every original name, alias, email,
phone, LinkedIn URL, company host and place it knows about, and exits 1 if any survive. When the real
data gains a new company, add it to `COMPANIES` in `tools/make_anonymized.py` (the script warns and
falls back to a generated name until you do). Add any other identifying phrase to `EXTRA`.

## Making a test PDF

1. Serve the site locally, e.g. `python3 -m http.server 8765`.
2. Open `http://localhost:8765/resume-develop.html?data=anon` (add `tier=`, slider or `clientid=`
   parameters as needed).
3. Click **Export PDF** and save. The file is named `Jordan-R-Ellison-Resume-<Tier>-YYYY-MM-DD.pdf`.

## Parser demo sites (manual uploads, anonymized PDFs only)

Status as of 2026-11-02, from `reports/ATS page usability 2026-11.md` in the Work vault.
Upload **only** a PDF made with `?data=anon`.

| Order | Site | Sign-up | Notes |
|---|---|---|---|
| 1 | [Affinda Free Resume Parser](https://www.affinda.com/free-resume-parser) | No | Commercial engine; about 150 parses a day without an account |
| 2 | [OpenResume parser](https://www.open-resume.com/resume-parser) | No | Runs in your browser; single-column English PDFs |
| 3 | [Textkernel Tx trial](https://cloud.textkernel.com/tx/console/register) | Yes (500 credits) | The engine behind Bullhorn, reportedly SAP SuccessFactors; the most ATS-relevant |
| 4 | Ashby application autofill: any `jobs.ashbyhq.com/<company>` posting | No | A real employer's form: **upload, look at the autofilled fields, never submit** |
| 5 | Lever autofill (`jobs.lever.co/<company>`); Workday "Autofill with Resume" | Lever no; Workday needs a per-employer account | Same caveat as row 4 |
| 6 | [Eden AI](https://www.edenai.co) | Yes | Runs several engines (Affinda, HireAbility, others) side by side; sends data to all of them |

RChilli and Daxtra offer sales demos or trials only.

For each upload, record: the site, the date, the page URL parameters used, and whether the parser got
the name, email, location, every position's title, employer and both dates, and the section headings.
