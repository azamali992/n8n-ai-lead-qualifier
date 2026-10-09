"""Send a handful of realistic leads to your webhook, for testing or for a demo recording.

    python scripts/send_test_leads.py https://your-n8n.example.com/webhook/lead-intake
    python scripts/send_test_leads.py http://localhost:5678/webhook-test/lead-intake --pause 4

Use the /webhook-test/ URL while the workflow is open in the editor (click
"Execute workflow" first), or the /webhook/ URL once it's published.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

LEADS = [
    {"name": "Dana Ruiz", "email": "dana@harbor-dental.example", "company": "Harbor Dental Group",
     "message": "We run 6 dental clinics and need a patient portal with online booking and an AI assistant "
                "for common questions. Budget is around $25,000 and we want it live by March."},
    {"name": "Mark Ellis", "email": "mark@ellisplumbing.example", "company": "Ellis Plumbing",
     "message": "Thinking about maybe redoing our website at some point. Not sure on budget yet."},
    {"name": "Sam", "email": "sam.student@example.com",
     "message": "Just curious how much a one page site costs, I'm a student."},
    {"name": "Growth Team", "email": "outreach@seo-boost.example",
     "message": "Hi! We offer high DA backlinks and guest posts to boost your SEO. Reply for our price list."},
    {"name": "Lena Hoffmann", "email": "lena@kraftwerk-logistics.example", "company": "Kraftwerk Logistics",
     "message": "Our ops team re-types delivery notes into our ERP every day, about 200 a day. We want this "
                "automated with AI. We have budget approved for Q1, can we talk this month?"},
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between leads")
    args = ap.parse_args()
    for lead in LEADS:
        req = urllib.request.Request(args.url, json.dumps(lead).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                print(f"{r.status}  {lead['email']:<38} {r.read().decode()[:120]}")
        except urllib.error.HTTPError as e:
            print(f"{e.code}  {lead['email']:<38} {e.read().decode()[:120]}")
        time.sleep(args.pause)


if __name__ == "__main__":
    main()
