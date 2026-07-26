//@version=5
// ═══════════════════════════════════════════════════════════════════════════════
// Relative Measured Volatility (RMV)
// © 2026 Triyambak — released for personal/educational use on TradingView.
// Inspired by the broader concept of tightness oscillators; formula and
// methodology independently designed and validated for VCP-style setups on NSE.
//
// What it is:
//   A 0–100 oscillator that measures how "loud" today's bar is versus the
//   strongest of the last few prior bars. Lower = tighter (compression);
//   higher = expansion. Built for spotting VCP-style contraction on daily
//   charts, but works on any liquid name and any timeframe you trust.
//
// How to read it:
//   • RMV ≤ 20  → very tight (today is well below recent baseline)
//   • RMV ≈ 50  → neutral (today equals the baseline)
//   • RMV ≥ 75  → expansion bar
//   • RMV = 100 → full expansion (capped)
//
// Signal tiers shown via background colour + rank digit at top of pane:
//   ➊ Confluence (A+ AND trough)   ➋ A+ only
//   ➌ Trough only                  ➍ Viable but no signal (RMV ≤ warm)
//
// Recommended timeframe: Daily. Pairs with your usual price-pane MAs / RS work.
// ═══════════════════════════════════════════════════════════════════════════════
indicator("Relative Measured Volatility (RMV)", shorttitle="RMV", overlay=false, max_lines_count=50, max_labels_count=50)

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 1 — Core RMV engine
// ─────────────────────────────────────────────────────────────────────────────
g_core             = "Core RMV engine"

atrLen             = input.int(3, minval=2, title="ATR baseline length (prior N bars)", group=g_core,
     tooltip="Window for the tightness baseline, computed over the LAST N PRIOR bars (today excluded).\nRMV uses max(ATR(N)[1], MAX(H-L, N)[1]) as the denominator — today is compared to the strongest recent prior bar.\nDefault 3 — matches the Tightness scan's ATR(3) horizon.\n• N=3 = matches Chartink Tightness scan (default)\n• N=5 = 1 trading week, smoother\n• N=8 = broader context")

closePosThreshold  = input.int(50, minval=1, maxval=99, title="Strong rejection threshold (%)", group=g_core,
     tooltip="Used by the 'rejection-bar' check.\nA bar qualifies as a strong rejection only when BOTH the open AND the close land in the upper portion of the day's range — i.e., openPos > X% AND closePos > X%.\nReason: a long lower wick with a strong close represents intraday rejection of weakness — but only when the open was already up there too (otherwise it's a 'rally from the bottom' which is real expansion, not tightness).\n\nDefault 50% (both endpoints in upper half). Raise to 60–70% for stricter qualification.")

absTightAtrLen     = input.int(5, minval=2, title="Absolute-tightness ATR length (prior N bars)", group=g_core,
     tooltip="Window for the 'is today's range small in absolute terms?' check.\nIf today's range ≤ multiplier × ATR(N) of last N prior bars, the bar is treated as absolutely tight — formula uses body as numerator regardless of open/close position.\nDefault 5 — short enough to track regime changes, smooth enough to avoid noise.")

absTightMult       = input.float(0.75, minval=0.1, step=0.05, title="Absolute-tightness ATR multiplier", group=g_core,
     tooltip="Strictness of the absolute-tightness gate. Today's range must be ≤ multiplier × ATR(absTightAtrLen) prior to qualify.\n• 1.0 — bar at or below recent ATR counts (most lenient)\n• 0.75 — bar must be 25%+ smaller than recent ATR (default — sweet spot)\n• 0.7 — stricter — bar must be 30%+ smaller\n• 0.5 — very strict — only sharply small bars qualify\nDefault 0.75.")

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 2 — Thresholds & zones
// ─────────────────────────────────────────────────────────────────────────────
g_zones            = "Thresholds & zones"

veryTightLevel     = input.int(10, minval=1, maxval=99, title="Very-tight level", group=g_zones,
     tooltip="Threshold used by the 'very tight' crossunder alert. Default 10.")

tightLevel         = input.int(15, minval=1, maxval=99, title="Tight level", group=g_zones,
     tooltip="Upper bound of the tight zone. Used for:\n• A+ setup eligibility (built-in RMV gate)\n• Trough / streak tracking\n• 'Tight-zone entry' alert")

warmLevel          = input.int(20, minval=1, maxval=99, title="Warm level", group=g_zones,
     tooltip="Upper bound of histogram visibility and rank ➍ (viable / no signal) background.\nRMV ≤ warm = histogram bars drawn; above warm = no bars and no background.")

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 3 — A+ setup (driven by Chartink scan logic)
// ─────────────────────────────────────────────────────────────────────────────
g_aplus            = "A+ setup"

showAplus          = input.bool(true, title="Show A+ setup marker", group=g_aplus,
     tooltip="Plots the gold A+ marker on the RMV pane when the chosen combination of Chartink scan conditions is met on a bar.")

aplusMode          = input.string("OR (either scan)", title="A+ trigger mode",
     options=["AND (both scans)", "OR (either scan)", "Tightness only", "VDU only"], group=g_aplus,
     tooltip="Which Chartink scan combination must hit for A+ to fire:\n• OR (either scan) — DEFAULT, most lenient, either scan passes\n• AND (both scans) — strictest, full Tightness AND full VDU pass on the same bar (rare confluence)\n• Tightness only — only the Tightness scan must pass\n• VDU only — only the VDU scan must pass\n\nA+ ALSO always requires RMV ≤ tight level (default 15) — the RMV-tightness gate is built in. Both scan formulas are mirrored from the user's Chartink scans (universe filters and the weekly-volume surge clause are intentionally omitted).")

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 4 — Tightness recognition aids
// ─────────────────────────────────────────────────────────────────────────────
g_tight            = "Tightness recognition"

smoothLen          = input.int(5, minval=1, title="RMV smoothing (bars)", group=g_tight,
     tooltip="Length of the SMA applied to RMV for the smoothed line.\nFilters single-bar dips so you only see structurally tight compression.\nDefault 5 = 1 trading week.")

showSmooth         = input.bool(true, title="Show smoothed RMV line", group=g_tight,
     tooltip="Plots SMA(RMV, smoothLen) as a thin secondary line.\nWhen this line stays below the tight level, you have *structural* tightness, not noise.")

showStreakLabel    = input.bool(true, title="Show tight-streak label", group=g_tight,
     tooltip="Adds streak info to the live RMV label: how many consecutive bars RMV has been below the tight level, plus the lowest RMV value during the streak.\nShown only while the current bar is in the tight regime.")

troughLookback     = input.int(5, minval=2, title="Trough lookback (bars)", group=g_tight,
     tooltip="Window for the 'lowest-in-N-bars' trough detection.\nBars where RMV equals its N-bar low get the rank ➌ mint background — marks the deepest compression of each cycle. Often these troughs precede the breakout by 1–3 bars.")

showTrough         = input.bool(true, title="Enable trough detection (rank ➌)", group=g_tight,
     tooltip="When ON, bars where RMV is the lowest reading over the trough lookback are recognised as 'trough' bars and get the rank ➌ mint background.\nWhen OFF, those bars fall to rank ➍ (viable / no signal) regardless of compression depth.\nNo dots/markers are drawn — the trough state is conveyed entirely through the background colour and the rank digit at top of pane.")

showRanks          = input.bool(true, title="Show rank labels (1/2/3/4) on top of pane", group=g_tight,
     tooltip="Plots a single digit at the top of the RMV pane on each bar, labelling the signal tier:\n• 1 = Confluence (A+ AND trough)\n• 2 = A+ only\n• 3 = Trough only\n• 4 = Viable but no signal (RMV ≤ warm level)\nUseful for screenshots, colour-blind reading, and quick scanning.")


// (Chartink scan setups — Tightness + VDU — drive the A+ marker via the mode dropdown above.
//  Their formulas are mirrored from the user's Chartink scans and computed in the body of the script.
//  Universe filters (price/mcap/liquidity) and the weekly-volume surge clause are intentionally omitted.)

// ─────────────────────────────────────────────────────────────────────────────
// GROUP 5 — Colours
// ─────────────────────────────────────────────────────────────────────────────
g_col              = "Colours"

themePreset        = input.string("Dark (default)", title="Theme preset",
     options=["Dark (default)", "Light"], group=g_col,
     tooltip="Switches all colours to a coordinated palette suited for the chosen TradingView theme.\n• Dark — pastel coral/yellow/mint on dark bg; white histogram + soft pastel-blue smoothed line + white rank digits\n• Light — muted coral/ochre/sage on light bg; charcoal histogram + denser pastel-blue smoothed line + dark slate rank digits\n\nWhen 'Light' is selected, the colour swatches below are IGNORED — the script uses the built-in light-theme palette. Pick 'Dark' if you want to keep using your custom swatches.")

colHistBright      = input.color(color.new(color.white, 0),  title="Histogram bar colour (dark theme)", group=g_col,
     tooltip="Histogram colour when RMV ≤ warm level (≤ 20). Above warm, no histogram bars are drawn.\nUsed when theme = Dark.")

colSmooth          = input.color(color.new(#C0D7F5, 39), title="Smoothed RMV line (dark theme)", group=g_col,
     tooltip="Colour of the SMA(RMV) overlay line. Default soft pastel blue @ 61% opacity.\nUsed when theme = Dark.")

colTrough          = input.color(color.new(#B5EAD7, 75), title="Background — trough bar (rank 3, dark theme)", group=g_col,
     tooltip="RMV-pane background fill on bars where RMV is the lowest in N bars (trough). Default pastel mint @ 75% transparency.\nUsed when theme = Dark.")

colAplus           = input.color(color.new(#FFF1A1, 60), title="Background — A+ bar (rank 2, dark theme)", group=g_col,
     tooltip="RMV-pane background fill on bars where A+ fires (per the chosen scan mode). Default pastel butter yellow @ 60% transparency.\nUsed when theme = Dark.")

colConfluence      = input.color(color.new(#FFB7B2, 40), title="Background — A+ + trough confluence (rank 1, dark theme)", group=g_col,
     tooltip="RMV-pane background fill on bars where BOTH A+ AND the 20-bar trough fire simultaneously. Default pastel coral @ 40% transparency.\nUsed when theme = Dark.")

colViable          = input.color(color.new(#E8E8E8, 90), title="Background — viable / no signal (rank 4, dark theme)", group=g_col,
     tooltip="RMV-pane background fill on bars where RMV ≤ warm level but no A+ or trough is firing. Default very-light grey @ 90% transparency.\nUsed when theme = Dark.")

colRank            = input.color(color.new(color.white, 30), title="Rank label colour (dark theme)", group=g_col,
     tooltip="Colour of the rank-tier digits plotted at the top of the pane. Default white @ 30% transparency.\nUsed when theme = Dark.")

// ── Light-theme palette (hard-coded; activated when themePreset = "Light") ──
isLight             = themePreset == "Light"
lightHistBright     = color.new(#374151, 0)              // charcoal slate — visible on white bg
lightSmooth         = color.new(#4A6FA5, 39)             // medium-dark pastel blue (61% opacity)
lightTrough         = color.new(#88B098, 75)             // muted sage green — rank 3
lightAplus          = color.new(#D4B448, 65)             // muted goldenrod — rank 2
lightConfluence     = color.new(#E8806E, 50)             // muted coral/salmon — rank 1
lightViable         = color.new(#A8B5C4, 72)             // soft cool grey-blue — rank 4 (subtle but readable on white)
lightRank           = color.new(#1F2937, 30)             // dark slate — readable on light bg

// Effective colours used by all plots/bgcolors below
fxHist             = isLight ? lightHistBright : colHistBright
fxSmooth           = isLight ? lightSmooth     : colSmooth
fxTrough           = isLight ? lightTrough     : colTrough
fxAplus            = isLight ? lightAplus      : colAplus
fxConfluence       = isLight ? lightConfluence : colConfluence
fxViable           = isLight ? lightViable     : colViable
fxRank             = isLight ? lightRank       : colRank


// ─────────────────────────────────────────────────────────────────────────────
// CORE CALCULATION
// ─────────────────────────────────────────────────────────────────────────────

// RMV — hybrid body/range formula with absolute-tightness gate + rejection check.
//
//   Numerator selection (today's "size"):
//     1. If today's range ≤ absTightMult × ATR(absTightAtrLen)[1]  → use BODY (small in absolute terms)
//     2. Else if openPos > thresh AND closePos > thresh             → use BODY (rejection bar at top)
//     3. Otherwise                                                   → use RANGE (real expansion)
//
//   Denominator (recent baseline, today EXCLUDED — prior atrLen bars only):
//     max(ATR(atrLen)[1], MAX(high−low, atrLen)[1])
//     Loud-anchor + today-exclusive — today is compared to the strongest recent prior bar
//     so expansion bars don't self-cancel by inflating their own baseline.
//
//   ratio = numerator / denominator
//   RMV   = min(ratio × 50, 100)
//
// Reading guide:
//   • RMV ≤ 20  → very tight (today is well below recent baseline)
//   • RMV ≈ 50  → neutral (today equals baseline)
//   • RMV ≥ 75  → expanding
//   • RMV = 100 → full expansion (capped: ratio ≥ 2)

rangeToday         = high - low
bodyToday          = math.abs(open - close)
rangeSafe          = math.max(rangeToday, syminfo.mintick)
openPosPct         = (open  - low) / rangeSafe * 100
closePosPct        = (close - low) / rangeSafe * 100

// Hybrid numerator selection
absTightAtrPrior   = ta.atr(absTightAtrLen)[1]
isAbsTight         = rangeToday <= absTightAtrPrior * absTightMult
strongOC           = (openPosPct > closePosThreshold) and (closePosPct > closePosThreshold)
useBody            = isAbsTight or strongOC
numerator          = useBody ? bodyToday : rangeToday

atrPrior           = ta.atr(atrLen)[1]
maxHlPrior         = ta.highest(rangeToday, atrLen)[1]
denominator        = math.max(atrPrior, maxHlPrior)

epsilon            = math.max(0.001, close * 0.0001)
ratio              = numerator / math.max(denominator, epsilon)
rmv                = math.min(ratio * 50, 100)

// Smoothed RMV — filters single-bar dips
rmvSmooth          = ta.sma(rmv, smoothLen)

// Tight-regime tracking — used by the live label and streak tracking
tightRegime        = rmv < tightLevel
barsInRegime       = ta.barssince(not tightRegime)
barsInRegimeSafe   = na(barsInRegime) ? bar_index : barsInRegime
var float streakMin = na
streakMin         := tightRegime ? (na(streakMin) ? rmv : math.min(streakMin, rmv)) : na

// Tightness trough — bars where RMV is the lowest in N bars
isTrough           = rmv == ta.lowest(rmv, troughLookback)

// ─────────────────────────────────────────────────────────────────────────────
// CHARTINK SCAN SETUPS — Tightness + VDU (used by A+ trigger logic below)
// ─────────────────────────────────────────────────────────────────────────────
// Shared helpers (computed once, used by both setups)
ema7s              = ta.ema(close, 7)
ema10s             = ta.ema(close, 10)
ema21s             = ta.ema(close, 21)
ema50s             = ta.ema(close, 50)
ema200s            = ta.ema(close, 200)
pctChg             = (close - close[1]) / close[1] * 100
pctChgPrev         = (close[1] - close[2]) / close[2] * 100
notRunaway         = not (math.sum(open  == low  ? 1 : 0, 10) >= 3 and
                           math.sum(close == high ? 1 : 0, 10) >= 3)
todayPctLe4        = pctChg <= 4

// ── Tightness setup (mirrors Chartink Tightness scan, sans universe filters) ──
tightnessSetup     = (high - low) <= ta.atr(3)
     and low   <= ema10s * 1.04
     and close >= ema10s * 0.99
     and close >= ema21s and close >= ema50s and close >= ema200s
     and notRunaway
     and close >= open * 0.985
     and todayPctLe4
     and pctChg     >= -1
     and pctChgPrev <= 7.5
     and (pctChg + pctChgPrev) <= 7
     and close >= ta.highest(high[1], 7) * 0.9
     and math.sum(close > close[1] ? 1 : 0, 9) != 9

// ── VDU setup (mirrors Chartink VDU scan, sans universe filters + weekly surge) ──
volEma21s          = ta.ema(volume, 21)
isVdu              = volume    <= volEma21s * 0.5
                  or volume[1] <= volEma21s * 0.5
                  or volume[2] <= volEma21s * 0.6
                  or volume == ta.lowest(volume, 90)
                  or volume == ta.lowest(volume, 365)

vduSetup           = isVdu
     and ema21s >= ema50s * 1.03
     and ema50s >= ema200s
     and close  <= ema10s * 1.06
     and notRunaway
     and todayPctLe4
     and pctChg     >= -2
     and pctChgPrev <= 4
     and pctChgPrev >  -3
     and ema10s > ema10s[3]
     and ema7s  > ema10s and ema10s > ema21s and ema21s > ema50s and ema50s > ema200s
     and math.sum(pctChg <= -5 ? 1 : 0, 7) == 0

// ── A+ trigger — driven by user-selected mode + built-in RMV ≤ tight gate ──
aPlusRaw           = aplusMode == "AND (both scans)" ? (tightnessSetup and vduSetup) :
                      aplusMode == "OR (either scan)" ? (tightnessSetup or  vduSetup) :
                      aplusMode == "Tightness only"   ?  tightnessSetup                :
                                                          vduSetup
aPlusSetup         = showAplus and aPlusRaw and rmv <= tightLevel

// ─────────────────────────────────────────────────────────────────────────────
// PLOTS
// ─────────────────────────────────────────────────────────────────────────────

// State-encoded background — priority chain (4 tiers):
//   • Rank 1: Confluence (A+ AND trough) → coral
//   • Rank 2: A+ only                     → yellow
//   • Rank 3: Trough only                 → mint
//   • Rank 4: Viable (RMV ≤ warm)         → light grey
//   • Else (RMV > warm)                   → no fill
isTroughActive = showTrough and isTrough
rankTier       = aPlusSetup and isTroughActive ? 1 :
                  aPlusSetup                    ? 2 :
                  isTroughActive                ? 3 :
                  rmv <= warmLevel              ? 4 : 0
bgState        = rankTier == 1 ? fxConfluence :
                  rankTier == 2 ? fxAplus      :
                  rankTier == 3 ? fxTrough     :
                  rankTier == 4 ? fxViable     : na
bgcolor(bgState, title="State background")

// Rank-tier circled digit at top of pane (one glyph per bar, only on tiers 1–4)
plotchar(showRanks and rankTier == 1 ? 95 : na, char="➊", title="Rank 1 (confluence)",
     location=location.absolute, color=fxRank, size=size.tiny)
plotchar(showRanks and rankTier == 2 ? 95 : na, char="➋", title="Rank 2 (A+ only)",
     location=location.absolute, color=fxRank, size=size.tiny)
plotchar(showRanks and rankTier == 3 ? 95 : na, char="➌", title="Rank 3 (trough only)",
     location=location.absolute, color=fxRank, size=size.tiny)
plotchar(showRanks and rankTier == 4 ? 95 : na, char="➍", title="Rank 4 (viable, no signal)",
     location=location.absolute, color=fxRank, size=size.tiny)

// Histogram — plain white when RMV ≤ warm, hidden above
plot(rmv, title="RMV", style=plot.style_histogram,
     color=rmv <= warmLevel ? fxHist : na, linewidth=2)

// Smoothed RMV — always drawn (gated only by the showSmooth toggle).
plot(rmvSmooth, title="RMV smoothed",
     color=showSmooth ? fxSmooth : na, linewidth=1)

// ─────────────────────────────────────────────────────────────────────────────
// LIVE LABEL
// ─────────────────────────────────────────────────────────────────────────────
var label rmvLabel = label.new(na, na, "", style=label.style_none, textcolor=color.white, size=size.small, yloc=yloc.price)
if barstate.islast
    txt = "RMV: " + str.tostring(rmv, '#.##')
    if showStreakLabel and tightRegime
        txt := txt + "\nTight: " + str.tostring(barsInRegimeSafe + 1) + " bars"
                   + "\nMin: "   + str.tostring(streakMin, '#.##')
    label.set_xy(rmvLabel, bar_index + 4, rmv)
    label.set_text(rmvLabel, txt)

// ─────────────────────────────────────────────────────────────────────────────
// ALERTS
// ─────────────────────────────────────────────────────────────────────────────
alertcondition(ta.crossunder(rmv, tightLevel),     title="RMV — tight zone entry",
     message="RMV crossed DOWN through tight level on {{ticker}}")

alertcondition(ta.crossunder(rmv, veryTightLevel), title="RMV — very tight",
     message="RMV crossed DOWN through very-tight level on {{ticker}}")

alertcondition(aPlusSetup and not aPlusSetup[1], title="RMV — A+ setup formed",
     message="A+ setup hit on {{ticker}} (per A+ mode: Tightness/VDU/AND/OR)")

// ─────────────────────────────────────────────────────────────────────────────
// PINE SCREENER EXPORTS
// ─────────────────────────────────────────────────────────────────────────────
// Boolean conditions exposed as 1/0 columns visible in the Data Window and
// consumable by TradingView Pine Screener. display=display.data_window keeps
// these out of the chart pane so they don't clutter the visual.
//
// In Pine Screener: filter on the column = 1 to find stocks matching the scan.

scanRmvVeryTight   = rmv <= veryTightLevel ? 1 : 0
scanAplus          = aPlusSetup            ? 1 : 0
scanRank1          = rankTier == 1         ? 1 : 0
scanRank2          = rankTier == 2         ? 1 : 0
scanRank3          = rankTier == 3         ? 1 : 0
scanRank4          = rankTier == 4         ? 1 : 0
scanBottomBreakout = (ta.lowest(rmvSmooth, 5) <= tightLevel) and rmv >= 50 ? 1 : 0
scanTightness      = tightnessSetup        ? 1 : 0
scanVdu            = vduSetup              ? 1 : 0
scanBothScans      = (tightnessSetup and vduSetup) ? 1 : 0

plot(scanRmvVeryTight,   title="Scan: RMV ≤ very tight",        display=display.data_window)
plot(scanAplus,          title="Scan: A+ setup",                display=display.data_window)
plot(scanRank1,          title="Scan: Rank 1 (confluence)",     display=display.data_window)
plot(scanRank2,          title="Scan: Rank 2 (A+ only)",        display=display.data_window)
plot(scanRank3,          title="Scan: Rank 3 (trough only)",    display=display.data_window)
plot(scanRank4,          title="Scan: Rank 4 (viable, no signal)", display=display.data_window)
plot(scanBottomBreakout, title="Scan: Smoothed bottom + breakout", display=display.data_window)
plot(scanTightness,      title="Scan: Tightness pass",          display=display.data_window)
plot(scanVdu,            title="Scan: VDU pass",                display=display.data_window)
plot(scanBothScans,      title="Scan: Both scans pass",         display=display.data_window)
