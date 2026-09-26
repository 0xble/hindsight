"""Figure for vectorize-io/hindsight#4823: consolidation before/after fair group selection.

Data: /Users/brianle/.hermes/cache/scratch/issue4823_data.json (hourly, UTC, read-only
queries against memory_units for one bank). "Shared group" = facts in the shared/untagged
scope; "Tagged groups" = every other tag-set scope group. All points are measured.
"""

import json
import sys
from datetime import datetime

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

D = json.load(open("/Users/brianle/.hermes/cache/scratch/issue4823_data.json"))
LAST_FULL = "2026-09-26 22"  # 23:00 was a partial hour at query time; excluded


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H")


cons = [r for r in D["cons"] if r[0] <= LAST_FULL]
back = [r for r in D["back"] if r[0] <= "2026-09-26 23"]
ct = [ts(r[0]) for r in cons]
shared = [int(r[1]) for r in cons]
tagged = [int(r[2]) for r in cons]
bt = [ts(r[0]) for r in back]
b_shared = [int(r[1]) / 1000 for r in back]
b_tagged = [int(r[2]) / 1000 for r in back]

SWITCH = datetime(2026, 9, 26, 4, 7)
TAGGED = "#1f77b4"
SHARED = "#b0b0b0"


def mean_rate(start, end):
    v = [s + t for x, s, t in zip(ct, shared, tagged) if start <= x < end]
    return sum(v) / len(v)


before = mean_rate(datetime(2026, 9, 24, 12), datetime(2026, 9, 26, 4))
after = mean_rate(datetime(2026, 9, 26, 11), datetime(2026, 9, 27))

plt.rcParams.update({"font.size": 10})
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6.4), dpi=200, sharex=True, layout="constrained")
fig.suptitle(
    f"Fair group selection: {before:,.0f} → {after:,.0f} facts consolidated per hour",
    x=0.01,
    ha="left",
    fontsize=13,
    fontweight="bold",
)

w = 1 / 24 * 0.9
ax1.bar(ct, shared, width=w, color=SHARED, label="Shared group", align="edge")
ax1.bar(ct, tagged, width=w, bottom=shared, color=TAGGED, label="Tagged groups", align="edge")
ax1.set_title("Facts consolidated per hour", loc="left", fontsize=10.5)
ax1.set_ylim(0, 2600)
ax1.legend(loc="upper left", frameon=False)

ax2.plot(bt, b_shared, color=SHARED, lw=2.2)
ax2.plot(bt, b_tagged, color=TAGGED, lw=2.2)
ax2.set_title("Facts waiting (thousands)", loc="left", fontsize=10.5)
ax2.set_ylim(0, 60)
ax2.text(bt[-1], b_shared[-1] + 2.5, f"Shared {b_shared[-1]:.0f}k", color="#666", ha="right", fontsize=9.5)
ax2.text(bt[-1], b_tagged[-1] - 6, f"Tagged {b_tagged[-1]:.0f}k", color=TAGGED, ha="right", fontsize=9.5)
ax2.text(bt[0], b_tagged[0] - 6, f"Tagged {b_tagged[0]:.0f}k", color=TAGGED, ha="left", fontsize=9.5)

for ax in (ax1, ax2):
    ax.axvline(SWITCH, color="#d62728", lw=1.3, ls="--")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#eee", lw=0.8)
    ax.set_axisbelow(True)
ax1.text(SWITCH, 2450, "fair selection on ", color="#d62728", va="top", ha="right", fontsize=9.5)
for (s, e), m in (((datetime(2026, 9, 24, 12), datetime(2026, 9, 26, 3, 50)), before), ((datetime(2026, 9, 26, 11), datetime(2026, 9, 26, 23)), after)):
    ax1.hlines(m, s, e, color="#222", lw=1, ls=":")
ax1.text(datetime(2026, 9, 25, 6), before + 70, f"avg {before:,.0f}/h", fontsize=9.5)
ax1.text(datetime(2026, 9, 26, 16), after + 700, f"avg {after:,.0f}/h at parallelism 16", fontsize=9.5, ha="center")

ax2.xaxis.set_major_locator(mdates.HourLocator(byhour=[0]))
ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d, 00:00"))

fig.text(
    0.01,
    -0.005,
    "One bank, measured hourly (UTC). The shared group still runs one batch at a time (#4063).",
    fontsize=8.5,
    color="#555",
    ha="left",
    va="top",
)

fig.savefig(sys.argv[1], bbox_inches="tight")
