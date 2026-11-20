"""Check an episode: spoken length, speaker share, turn rules, and numbers missing from the fact cards.
usage: python check_script.py <week_dir> <n>"""
import re
import sys
from pathlib import Path

d, n = Path(sys.argv[1]), sys.argv[2]
SPEAKERS = ("照井", "しらべ", "わた")
script = (d / f"ep{n}-script.md").read_text(encoding="utf-8").split("---", 2)[2]
facts = (d / f"ep{n}-facts.md").read_text(encoding="utf-8")
lines = [ln for ln in script.splitlines() if re.match(r"^(照井|しらべ|わた):", ln)]
bad = [ln for ln in script.splitlines() if ln.strip() and not ln.startswith("## ") and not re.match(r"^(照井|しらべ|わた):", ln)]


def spoken(ln):
    return re.sub(r"\{([^|}]*)\|([^}]*)\}", r"\2", ln.split(":", 1)[1])


def plain(t):
    return re.sub(r"\{([^|}]*)\|([^}]*)\}", r"\1", t)


tot = sum(len(spoken(ln)) for ln in lines)
print(f"spoken chars: {tot}  (~{tot / 300:.1f} min, guide ~6000 / 20 min; may run over (nobody stops teru) or stop early at a good break)")
print("share %:", {s: round(sum(len(spoken(ln)) for ln in lines if ln.startswith(s + ":")) / tot * 100) for s in SPEAKERS},
      "(target teru 45-50 / shirabe 25-30 / wata 20-25)")
run = mx = 1
for a, b in zip(lines, lines[1:]):
    run = run + 1 if a.split(":")[0] == b.split(":")[0] else 1
    mx = max(mx, run)
print("max consecutive lines by one speaker:", mx, "(must be <= 2)")
long_lines = [spoken(ln)[:30] for ln in lines if len(spoken(ln)) > 200]
print("lines over 200 chars:", len(long_lines), long_lines)
print("lines without a speaker label:", len(bad), [b[:30] for b in bad])
wata = [ln for ln in lines if ln.startswith("わた:")]
print("wata first-person (should be ~0):", sum(ln.count("俺") for ln in wata),
      "| rough endings (だな/だ。/ダメ):", sum(bool(re.search(r"だな|だ。|ダメ|じゃないですか", ln)) for ln in wata))
nums = lambda s: set(re.findall(r"\d+(?:\.\d+)?", s))
for name, text in (("script", plain(script)), ("article", plain((d / f"ep{n}-article.md").read_text(encoding="utf-8"))
                                                         if (d / f"ep{n}-article.md").exists() else "")):
    print(f"{name}: numbers not in facts:", sorted(nums(text) - nums(facts), key=float))
