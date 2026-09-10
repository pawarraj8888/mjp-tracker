import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.store import Store  # noqa: E402


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def sample_state():
    """A small synthetic tracker state: two awarded tenders (one contractor
    wins both, under a spelling variant, so it must merge) and one live-only
    tender."""
    live_rows = [{
        "tender_id": "2026_JALGA_9001_1",
        "title": "Water supply pipeline at Chopda under DPDC",
        "ref_no": "ZP/JAL/01", "published": "01-Jul-2026 10:00 AM",
        "closing": "20-Jul-2026 03:00 PM", "opening": "21-Jul-2026 11:00 AM",
        "org_chain": "Maharashtra||RDD||RDD-CEO-JALGAON",
        "source": "ZP Jalgaon DPDC", "sources": ["ZP Jalgaon DPDC"],
        "url": "https://mahatenders.gov.in/x",
    }]
    seen = {
        "2026_JALGA_9002_1": {
            "title": "Road strengthening at Bhusawal",
            "ref_no": "PWD/BSL/2", "closing": "10-Jun-2026 03:00 PM",
            "opening": "11-Jun-2026 11:00 AM", "published": "01-Jun-2026 10:00 AM",
            "org_chain": "Maharashtra||PWD||EE PWD Division Jalgaon",
            "source": "Jalgaon statewide", "first_seen": "2026-06-01T00:00:00",
        },
    }
    details = {
        "2026_JALGA_9001_1": {
            "Tender ID": "2026_JALGA_9001_1", "Tender Category": "Works",
            "Location": "Chopda Dist Jalgaon", "Tender Value in ₹": "1,45,00,000",
            "Product Category": "Civil Works",
        },
    }
    awards = {
        "2026_JALGA_9002_1": {
            "fields": {"Tender ID": "2026_JALGA_9002_1", "Title": "Road strengthening at Bhusawal",
                       "Organisation Chain": "Maharashtra||PWD||EE PWD Division Jalgaon",
                       "Tender Value in ₹": "60,00,000",
                       "Work Completion Period (in days) :": "90"},
            "award": {"contractor": "M/s Shree Constructions Pvt Ltd",
                      "awarded_value": 5900000, "contract_date": "01-Sep-2026",
                      "bidders": [{"name": "M/s Shree Constructions Pvt Ltd",
                                   "status": "Awarded", "value": "59,00,000"}]},
            "org": "EE PWD Division Jalgaon",
        },
        "2026_JALGA_9003_1": {
            "fields": {"Tender ID": "2026_JALGA_9003_1", "Title": "CC road at Raver",
                       "Organisation Chain": "Maharashtra||RDD||RDD-CEO-JALGAON",
                       "Tender Value in ₹": "24,00,000"},
            "award": {"contractor": "Shree Constructions", "awarded_value": 2400000,
                      "contract_date": "20-Jul-2026",
                      "bidders": [{"name": "Shree Constructions", "status": "Awarded"}]},
            "org": "RDD-CEO-JALGAON",
        },
    }
    return {"live_rows": live_rows, "seen": seen, "details": details,
            "awards": awards}
