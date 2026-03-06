# CSV Playbook

## Quick checks

- Preview rows: `head -n 10 /mnt/data/your-file.csv`.
- Count rows:

```bash
python - <<'PY'
import csv

with open('/mnt/data/your-file.csv', newline='') as f:
    print(sum(1 for _ in csv.DictReader(f)))
PY
```

## Grouped totals template

```bash
python - <<'PY'
import csv
from collections import defaultdict

totals = defaultdict(float)
with open('/mnt/data/your-file.csv', newline='') as f:
    for row in csv.DictReader(f):
        totals[row['region']] += float(row['amount'])

for region in sorted(totals):
    print(region, round(totals[region], 2))
PY
```
