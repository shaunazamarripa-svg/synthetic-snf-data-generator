#!/usr/bin/env python3
"""
Synthetic skilled nursing facility (SNF) data generator.

Produces a small, fully fictional star schema (facilities, payers, residents,
stays, ADT events, daily census, invoices) for demos, training, and testing
BI / lakehouse patterns without ever touching PHI.

Design rules:
  * Zero real data. Residents are numbered placeholders ("Resident 000123").
  * No dates of birth, addresses, SSNs, MRNs, or free text. Age is a band.
  * Deterministic: the same --seed always yields the same files.
  * Standard library only (Python 3.9+).

Usage:
  python generate_snf_data.py --seed 42 --facilities 3 \
      --start 2025-01-01 --end 2025-12-31 --out output
"""
import argparse
import csv
import math
import os
import random
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- reference data
# id, name, category, daily_rate, lag_min_days, lag_max_days, short_pay_probability
PAYERS = [
    ("PAY-01", "Medicare Part A", "Medicare", 620.0, 10, 25, 0.03),
    ("PAY-02", "Medicare Advantage", "Medicare Advantage", 540.0, 25, 60, 0.08),
    ("PAY-03", "Medicaid", "Medicaid", 245.0, 30, 75, 0.05),
    ("PAY-04", "Managed Care", "Managed Care", 480.0, 30, 70, 0.07),
    ("PAY-05", "Private Pay", "Private Pay", 285.0, 5, 35, 0.02),
    ("PAY-06", "Veterans / Other", "Other", 400.0, 20, 60, 0.04),
]
PAYER_IDS = [p[0] for p in PAYERS]
PAYER_BY_ID = {p[0]: p for p in PAYERS}

# Payer mix differs for short-stay (rehab) vs long-term residents.
PAYER_WEIGHTS = {
    "short": [0.42, 0.30, 0.03, 0.18, 0.05, 0.02],
    "long": [0.00, 0.02, 0.72, 0.02, 0.22, 0.02],
}

# name, stay_type, weight, mean length of stay (days)
DIAGNOSIS_GROUPS = [
    ("Orthopedic Rehab", "short", 0.22, 22),
    ("Cardiac / Pulmonary", "short", 0.14, 18),
    ("Stroke / Neuro Rehab", "short", 0.10, 28),
    ("Wound / Infection Care", "short", 0.08, 24),
    ("Long-Term Care", "long", 0.38, 240),
    ("Memory Care", "long", 0.08, 300),
]

AGE_BANDS = ["55-64", "65-74", "75-84", "85+"]
AGE_WEIGHTS = {"short": [0.08, 0.32, 0.38, 0.22], "long": [0.05, 0.20, 0.35, 0.40]}

ADMISSION_SOURCES = [("Acute Hospital", 0.68), ("Home / Community", 0.20), ("Other Facility", 0.12)]
DISCHARGE_DISPOSITIONS = {
    "short": [("Home", 0.62), ("Assisted Living", 0.10), ("Hospital Readmission", 0.18), ("Transferred to Long-Term Care", 0.10)],
    "long": [("Hospital Readmission", 0.30), ("Transferred to Other Facility", 0.30), ("Home", 0.20), ("Assisted Living", 0.20)],
}

FACILITY_BEDS = [120, 96, 150, 110, 80, 132, 100, 90]
REGIONS = ["Region A", "Region B", "Region C"]


def wchoice(rng, pairs):
    """Weighted choice from [(value, weight), ...]."""
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights, k=1)[0]


def month_first(d):
    return d.replace(day=1)


def next_month_first(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


# ---------------------------------------------------------------- simulation
class Stay:
    __slots__ = (
        "stay_id", "resident_id", "facility_id", "payer_id", "admit_date", "planned_discharge",
        "discharge_date", "diagnosis_group", "stay_type", "admission_source", "discharge_disposition",
    )


def draw_stay(rng, stay_id, resident_id, facility_id, admit_date):
    dx_name, stay_type, _, mean_los = wchoice(
        rng, [((d[0], d[1], d[2], d[3]), d[2]) for d in DIAGNOSIS_GROUPS]
    )
    s = Stay()
    s.stay_id = stay_id
    s.resident_id = resident_id
    s.facility_id = facility_id
    s.payer_id = rng.choices(PAYER_IDS, weights=PAYER_WEIGHTS[stay_type], k=1)[0]
    s.admit_date = admit_date
    los = max(1, int(rng.lognormvariate(math.log(mean_los), 0.6)))
    s.planned_discharge = admit_date + timedelta(days=los)
    s.discharge_date = None
    s.diagnosis_group = dx_name
    s.stay_type = stay_type
    s.admission_source = wchoice(rng, ADMISSION_SOURCES)
    s.discharge_disposition = None
    return s


def simulate(rng, facilities, start, end):
    stays, residents = [], {}
    stay_seq = 0
    res_seq = 0
    census_rows = []

    def new_resident(stay_type):
        nonlocal res_seq
        res_seq += 1
        rid = "RES-%06d" % res_seq
        residents[rid] = {
            "resident_id": rid,
            "resident_label": "Resident %06d" % res_seq,
            "age_band": rng.choices(AGE_BANDS, weights=AGE_WEIGHTS[stay_type], k=1)[0],
            "sex": rng.choice(["F", "F", "M"]),  # coarse, synthetic
        }
        return rid

    for fac in facilities:
        beds = fac["licensed_beds"]
        active = []
        readmit_pool = []  # residents discharged earlier at this facility

        # Opening census: backdate admissions so day one is already populated.
        for _ in range(int(beds * rng.uniform(0.78, 0.88))):
            stay_seq += 1
            admit = start - timedelta(days=rng.randint(1, 120))
            s = draw_stay(rng, "STY-%07d" % stay_seq, None, fac["facility_id"], admit)
            s.resident_id = new_resident(s.stay_type)
            if s.planned_discharge <= start:  # avoid a same-day mass discharge
                s.planned_discharge = start + timedelta(days=rng.randint(1, 30))
            active.append(s)
            stays.append(s)

        d = start
        while d <= end:
            # discharges
            still = []
            for s in active:
                if s.planned_discharge == d:
                    s.discharge_date = d
                    s.discharge_disposition = wchoice(rng, DISCHARGE_DISPOSITIONS[s.stay_type])
                    readmit_pool.append(s.resident_id)
                else:
                    still.append(s)
            active = still

            # admissions toward a seasonal occupancy target
            doy = d.timetuple().tm_yday
            occ = 0.86 + 0.04 * math.sin(2 * math.pi * doy / 365.0) + rng.gauss(0, 0.012)
            occ = min(0.97, max(0.60, occ))
            need = int(beds * occ) - len(active)
            n_admit = 0 if need <= 0 else min(need, rng.choice([0, 1, 1, 2, 2, 3, 4]))
            for _ in range(n_admit):
                stay_seq += 1
                s = draw_stay(rng, "STY-%07d" % stay_seq, None, fac["facility_id"], d)
                if readmit_pool and rng.random() < 0.08:
                    s.resident_id = readmit_pool.pop(rng.randrange(len(readmit_pool)))
                else:
                    s.resident_id = new_resident(s.stay_type)
                active.append(s)
                stays.append(s)

            # end-of-day census by payer
            counts = {p: 0 for p in PAYER_IDS}
            for s in active:
                counts[s.payer_id] += 1
            for p in PAYER_IDS:
                census_rows.append((d, fac["facility_id"], p, counts[p], beds))
            d += timedelta(days=1)

        for s in active:  # still in-house after the window: leave discharge_date empty
            if s.planned_discharge <= end:
                s.discharge_date = s.planned_discharge
    return stays, residents, census_rows


def build_invoices(rng, stays, start, end):
    """Monthly invoices per stay, billed on the 5th of the following month."""
    rows, seq = [], 0
    for s in stays:
        rate = PAYER_BY_ID[s.payer_id][3]
        lag_min, lag_max, short_p = PAYER_BY_ID[s.payer_id][4:7]
        stay_end_excl = s.discharge_date if s.discharge_date else end + timedelta(days=1)
        m = month_first(max(s.admit_date, start))
        while m <= min(stay_end_excl, end):
            nm = next_month_first(m)
            p_start = max(m, s.admit_date, start)
            p_end_excl = min(nm, stay_end_excl)
            days = (p_end_excl - p_start).days
            inv_date = nm + timedelta(days=4)
            m = nm
            if days <= 0 or inv_date > end:
                continue  # nothing to bill yet
            seq += 1
            billed = round(days * rate * rng.uniform(0.97, 1.03), 2)
            paid_date = inv_date + timedelta(days=rng.randint(lag_min, lag_max))
            if paid_date <= end:
                paid = billed
                if rng.random() < short_p:
                    paid = round(billed * rng.uniform(0.60, 0.90), 2)
                status = "Paid" if paid >= billed else "Short-Paid"
                pdate = paid_date.isoformat()
            else:
                paid, status, pdate = 0.0, "Open", ""
            rows.append((
                "INV-%07d" % seq, s.stay_id, s.facility_id, s.payer_id,
                p_start.isoformat(), (p_end_excl - timedelta(days=1)).isoformat(), days,
                inv_date.isoformat(), "%.2f" % billed, "%.2f" % paid, pdate, status,
            ))
    return rows


def build_dim_date(start, end):
    rows, d = [], start
    while d <= end:
        rows.append((
            d.isoformat(), d.year, (d.month - 1) // 3 + 1, d.month, d.strftime("%B"),
            d.strftime("%A"), 1 if d.weekday() >= 5 else 0,
            month_first(d).isoformat(), (next_month_first(d) - timedelta(days=1)).isoformat(),
        ))
        d += timedelta(days=1)
    return rows


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print("  %-26s %8d rows" % (os.path.basename(path), len(rows)))


def main():
    ap = argparse.ArgumentParser(description="Generate synthetic SNF data (no PHI).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--facilities", type=int, default=3, help="1-8")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--out", default="output")
    a = ap.parse_args()

    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if end < start:
        ap.error("--end must be on or after --start")
    if not 1 <= a.facilities <= len(FACILITY_BEDS):
        ap.error("--facilities must be between 1 and %d" % len(FACILITY_BEDS))

    rng = random.Random(a.seed)
    facilities = [{
        "facility_id": "FAC-%02d" % (i + 1),
        "facility_name": "Sample Facility %02d" % (i + 1),
        "region": REGIONS[i % len(REGIONS)],
        "licensed_beds": FACILITY_BEDS[i],
    } for i in range(a.facilities)]

    stays, residents, census = simulate(rng, facilities, start, end)
    invoices = build_invoices(rng, stays, start, end)
    os.makedirs(a.out, exist_ok=True)
    o = lambda n: os.path.join(a.out, n)

    print("Writing to %s/ (seed=%d)" % (a.out, a.seed))
    write_csv(o("dim_facility.csv"), ["facility_id", "facility_name", "region", "licensed_beds"],
              [(f["facility_id"], f["facility_name"], f["region"], f["licensed_beds"]) for f in facilities])
    write_csv(o("dim_payer.csv"), ["payer_id", "payer_name", "payer_category", "synthetic_daily_rate"],
              [(p[0], p[1], p[2], "%.2f" % p[3]) for p in PAYERS])
    write_csv(o("dim_date.csv"),
              ["date", "year", "quarter", "month", "month_name", "day_of_week", "is_weekend", "month_start", "month_end"],
              build_dim_date(start, end))
    write_csv(o("dim_resident.csv"), ["resident_id", "resident_label", "age_band", "sex"],
              [(r["resident_id"], r["resident_label"], r["age_band"], r["sex"]) for r in residents.values()])
    write_csv(o("fact_stay.csv"),
              ["stay_id", "resident_id", "facility_id", "payer_id", "admit_date", "discharge_date",
               "diagnosis_group", "stay_type", "admission_source", "discharge_disposition"],
              [(s.stay_id, s.resident_id, s.facility_id, s.payer_id, s.admit_date.isoformat(),
                s.discharge_date.isoformat() if s.discharge_date else "", s.diagnosis_group, s.stay_type,
                s.admission_source, s.discharge_disposition or "") for s in stays])

    events, eseq = [], 0
    for s in stays:
        for etype, d in (("A01-Admit", s.admit_date), ("A03-Discharge", s.discharge_date)):
            if d is None or d < start or d > end:
                continue
            eseq += 1
            ts = datetime(d.year, d.month, d.day, rng.randint(6, 21), rng.choice([0, 15, 30, 45]))
            events.append(("EVT-%07d" % eseq, s.stay_id, s.resident_id, s.facility_id, etype, ts.isoformat()))
    events.sort(key=lambda r: r[5])
    write_csv(o("adt_events.csv"),
              ["event_id", "stay_id", "resident_id", "facility_id", "event_type", "event_timestamp"], events)
    write_csv(o("fact_census_daily.csv"),
              ["date", "facility_id", "payer_id", "census_count", "licensed_beds"],
              [(c[0].isoformat(), c[1], c[2], c[3], c[4]) for c in census])
    write_csv(o("fact_invoice.csv"),
              ["invoice_id", "stay_id", "facility_id", "payer_id", "service_start", "service_end", "billed_days",
               "invoice_date", "billed_amount", "paid_amount", "paid_date", "invoice_status"], invoices)
    print("Done. All data is synthetic.")


if __name__ == "__main__":
    main()
