"""Figure for vectorize-io/hindsight#4823: consolidation throughput before/after fair group selection.

Data: /Users/brianle/.hermes/cache/scratch/issue4823_data.json (hourly, UTC, read-only
queries against memory_units for one bank). "Largest group" = facts whose consolidation
scope is the shared/untagged scope; "other groups" = every other tag-set scope group.
"""

import json
import sys
from datetime import datetime

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

D = json.load(open("/Users/brianle/.hermes/cache/scratch/issue4823_data.json"))
LAST_FULL = "2026-09-26 22"  # 23:00 is a partial hour at query time; excluded


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H")


cons = [row for row in D["cons"] if row[0] <= LAST_FULL]
back = [row for row in D["back"] if row[0] <= "2026-09-26 23"]

ct = [ts(r[0]) for r in cons]
big = [int(r[1]) for r in cons]
other = [int(r[2]) for r in cons]
bt = [ts(r[0]) for r in back]
bbig = [int(r[1]) / 1000 for r in back]
bother = [int(r[2]) / 1000 for r in back]

SWITCH = datetime(2026, 9, 26, 4, 7)
P16 = datetime(2026, 9, 26, 10, 13)
ACCENT = "#1f77b4"
MUTED = "#9a9a9a"

fig, (ax1, ax2) = plt.subplots(
    2, 1, figsize=(9, 7.2), dpi=200, sharex=True, layout="constrained", height_ratios=[1.15, 1]
)

w = 1 / 24 * 0.9
ax1.bar(ct, big, width=w, color=MUTED, label="Largest scope group (shared scope)", align="edge")
ax1.bar(ct, other, width=w, bottom=big, color=ACCENT, label="All other scope groups", align="edge")
ax1.set_ylabel("Facts consolidated per hour")
ax1.set_title(
    "Consolidation throughput before and after fair group selection\n"
    "(one production bank, consolidation_llm_parallelism=16 before, 8 then 16 after)",
    fontsize=11,
    loc="left",
)
ax1.legend(loc="upper left", frameon=False, fontsize=9)
ax1.set_ylim(0, 2900)

pre_n = sum(1 for t in ct if t < SWITCH)
pre_avg = sum(b + o for t, b, o in zip(ct, big, other) if t < datetime(2026, 9, 26, 4)) / max(
    1, sum(1 for t in ct if t < datetime(2026, 9, 26, 4))
)
post = [(b + o) for t, b, o in zip(ct, big, other) if t >= datetime(2026, 9, 26, 11)]
post_avg = sum(post) / len(post)
pre_s, pre_e = datetime(2026, 9, 24, 12), datetime(2026, 9, 26, 3, 50)
post_s, post_e = datetime(2026, 9, 26, 11), datetime(2026, 9, 26, 23)
ax1.hlines(pre_avg, pre_s, pre_e, color="#333", lw=1.2, ls="--")
ax1.hlines(post_avg, post_s, post_e, color="#333", lw=1.2, ls="--")
ax1.text(datetime(2026, 9, 25, 0), pre_avg + 90, f"oldest-first fetch: mean {pre_avg:,.0f}/h", fontsize=10, color="#222")
ax1.text(datetime(2026, 9, 26, 10, 30), 2560, f"parallelism 16:\nmean {post_avg:,.0f}/h", fontsize=10, color="#222", ha="left", va="bottom")

ax2.plot(bt, bbig, color=MUTED, lw=2, label="Largest scope group (shared scope)")
ax2.plot(bt, bother, color=ACCENT, lw=2, label="All other scope groups")
ax2.set_ylabel("Unconsolidated facts (thousands)")
ax2.set_ylim(0, 60)
ax2.legend(loc="lower left", frameon=False, fontsize=9)
ax2.annotate(f"{bother[0]:.1f}k", xy=(bt[0], bother[0]), xytext=(4, -14), textcoords="offset points", fontsize=8, color=ACCENT)
ax2.annotate(f"{bother[-1]:.1f}k", xy=(bt[-1], bother[-1]), xytext=(-30, -14), textcoords="offset points", fontsize=8, color=ACCENT)
ax2.annotate(f"{bbig[-1]:.1f}k", xy=(bt[-1], bbig[-1]), xytext=(-30, 6), textcoords="offset points", fontsize=8, color="#555")

for ax in (ax1, ax2):
    ax.axvline(SWITCH, color="#d62728", lw=1.2, ls="--")
    ax.axvline(P16, color="#d62728", lw=0.8, ls=":")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#eee", lw=0.8)
    ax.set_axisbelow(True)

ax1.text(SWITCH, 2880, "fair selection on  \n(parallelism 8)  ", color="#d62728", fontsize=9, va="top", ha="right")
ax2.text(P16, 58, " parallelism 16", color="#d62728", fontsize=9, va="top", ha="left")

ax2.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 12]))
ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M"))
ax2.set_xlabel("UTC, hourly")

fig.text(
    0.01,
    -0.01,
    "Model openai/gpt-oss-20b, batch size 8. Every point is measured from the database; the last partial hour is omitted. "
    "New facts kept arriving throughout,\nso the backlog lines show net change. The shared group still runs one batch at a time "
    "(by design, #4063), which is why its backlog barely moves.",
    fontsize=7.5,
    color="#333",
    ha="left",
    va="top",
)

fig.savefig(sys.argv[1], bbox_inches="tight")
