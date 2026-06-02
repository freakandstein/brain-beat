#!/usr/bin/env python3
"""
Plot EEG session dari CSV log.

Usage:
    python3 plot_eeg.py               # pakai file sesi terbaru
    python3 plot_eeg.py eeg_session_*.csv  # file tertentu

Install dependensi jika belum:
    pip3 install matplotlib numpy
"""

import sys
import glob
import csv

try:
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.ticker as ticker
except ImportError:
    print("❌  Perlu install: pip3 install matplotlib numpy")
    sys.exit(1)

# ── Konstanta warna (sesuai UI) ───────────────────────────────────────────
BG        = '#0f0f1a'
PANEL     = '#1a1a2e'
BORDER    = '#2a2a4a'
TEXT      = '#e2e8f0'
DIM       = '#94a3b8'

STATE_COLORS = {
    'calm':  '#4ade80',
    'tense': '#c084fc',
}
BAND_COLORS = {
    'delta': '#fb923c',
    'theta': '#34d399',
    'alpha': '#60a5fa',
    'beta':  '#a78bfa',
}


# ── Load CSV ──────────────────────────────────────────────────────────────
def load_csv(path: str) -> list:
    rows = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append({
                    'time':    row['time'],
                    'elapsed': float(row['elapsed_s']),
                    'alpha':   float(row['alpha']),
                    'beta':    float(row['beta']),
                    'theta':   float(row['theta']),
                    'delta':   float(row['delta']),
                    'tbr':     float(row['tbr']),
                    'state':   row['state'].strip(),
                    'hr':      float(row['hr']) if row.get('hr', '').strip() else None,
                })
            except (ValueError, KeyError):
                continue
    return rows


# ── Styling helper ────────────────────────────────────────────────────────
def style_ax(ax):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=DIM, labelsize=8)
    ax.yaxis.label.set_color(DIM)
    ax.xaxis.label.set_color(DIM)
    for spine in ax.spines.values():
        spine.set_color(BORDER)


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    # Pilih file
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        files = sorted(glob.glob('eeg_session_*.csv'))
        if not files:
            print("❌  Tidak ada file CSV. Jalankan app EEG terlebih dahulu.")
            sys.exit(1)
        path = files[-1]

    print(f"📊  Membaca: {path}")
    data = load_csv(path)
    if len(data) < 3:
        print("❌  Data terlalu sedikit di CSV.")
        sys.exit(1)

    # Array numpy
    t      = np.array([r['elapsed'] for r in data])
    alpha  = np.array([r['alpha']   for r in data])
    beta   = np.array([r['beta']    for r in data])
    theta  = np.array([r['theta']   for r in data])
    delta  = np.array([r['delta']   for r in data])
    tbr    = np.array([r['tbr']     for r in data])
    states = [r['state'] for r in data]
    hrs    = [r['hr']    for r in data]

    total_sec = int(t[-1])
    n_focus   = states.count('focus')
    n_relax   = states.count('relax')
    n_stress  = states.count('stress')
    n_drowsy  = states.count('drowsy')

    # ── Layout ────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(13, 9), facecolor=BG)
    fig.suptitle(
        f'EEG Session Log — {path}\n'
        f'Durasi: {total_sec//60}m {total_sec%60}s  |  '
        f'Focus: {n_focus}s  Relax: {n_relax}s  Stress: {n_stress}s  Drowsy: {n_drowsy}s',
        color=TEXT, fontsize=11, fontweight='bold', y=0.98
    )

    gs = fig.add_gridspec(4, 1, hspace=0.45,
                          top=0.91, bottom=0.07, left=0.07, right=0.97,
                          height_ratios=[3, 2, 1.2, 1])

    ax_bands = fig.add_subplot(gs[0])
    ax_tbr   = fig.add_subplot(gs[1], sharex=ax_bands)
    ax_state = fig.add_subplot(gs[2], sharex=ax_bands)
    ax_hr    = fig.add_subplot(gs[3], sharex=ax_bands)

    for ax in [ax_bands, ax_tbr, ax_state, ax_hr]:
        style_ax(ax)
    plt.setp(ax_bands.get_xticklabels(), visible=False)
    plt.setp(ax_tbr.get_xticklabels(),   visible=False)
    plt.setp(ax_state.get_xticklabels(), visible=False)

    # ── Plot 1: EEG Bands ─────────────────────────────────────────────────
    ax_bands.plot(t, delta, color=BAND_COLORS['delta'], lw=1.2, alpha=0.85, label='δ delta')
    ax_bands.plot(t, theta, color=BAND_COLORS['theta'], lw=1.2, alpha=0.85, label='θ theta')
    ax_bands.plot(t, alpha, color=BAND_COLORS['alpha'], lw=1.6, label='α alpha')
    ax_bands.plot(t, beta,  color=BAND_COLORS['beta'],  lw=1.6, label='β beta')
    ax_bands.axhline(0.5, color=BORDER, lw=0.8, ls='--')
    ax_bands.set_ylim(-0.05, 1.10)
    ax_bands.set_ylabel('Normalized (0–1)', fontsize=8)
    ax_bands.set_title('EEG Band Powers', color=TEXT, fontsize=9, pad=4)
    ax_bands.legend(
        loc='upper right', fontsize=7.5,
        facecolor=PANEL, labelcolor=TEXT, framealpha=0.9,
        ncol=4, columnspacing=1.0
    )

    # ── Plot 2: TBR ───────────────────────────────────────────────────────
    ax_tbr.plot(t, tbr, color='#f472b6', lw=1.5, label='TBR')
    ax_tbr.axhline(0.52, color='#fbbf24', lw=1.0, ls='--', alpha=0.8)
    ax_tbr.axhline(0.72, color='#f87171', lw=0.8, ls=':',  alpha=0.7)
    ax_tbr.fill_between(t, tbr, 0.52, where=(tbr < 0.52),
                        alpha=0.18, color='#a78bfa', label='Focus zone (TBR < 0.52)')
    ax_tbr.fill_between(t, tbr, 0.72, where=(tbr > 0.72),
                        alpha=0.18, color='#f87171', label='Drowsy zone (TBR > 0.72)')
    ax_tbr.set_ylim(-0.05, 1.10)
    ax_tbr.set_ylabel('TBR  (θ/β)', fontsize=8)
    ax_tbr.set_title('Theta/Beta Ratio (Frontal)', color=TEXT, fontsize=9, pad=4)
    ax_tbr.text(t[-1] + 1, 0.52, '0.52', color='#fbbf24', fontsize=7, va='center')
    ax_tbr.text(t[-1] + 1, 0.72, '0.72', color='#f87171', fontsize=7, va='center')
    ax_tbr.legend(
        loc='upper right', fontsize=7,
        facecolor=PANEL, labelcolor=TEXT, framealpha=0.9,
        ncol=3, columnspacing=1.0
    )

    # ── Plot 3: State timeline ────────────────────────────────────────────
    for i in range(len(states) - 1):
        color = STATE_COLORS.get(states[i], DIM)
        ax_state.axvspan(t[i], t[i + 1], alpha=0.65, color=color, linewidth=0)
    if states:
        ax_state.axvspan(t[-1], t[-1] + 1, alpha=0.65,
                         color=STATE_COLORS.get(states[-1], DIM), linewidth=0)

    patches = [
        mpatches.Patch(color=c, label=s.capitalize(), alpha=0.75)
        for s, c in STATE_COLORS.items()
    ]
    ax_state.legend(handles=patches, loc='upper right', fontsize=7.5,
                    facecolor=PANEL, labelcolor=TEXT, framealpha=0.9,
                    ncol=4, columnspacing=0.8)
    ax_state.set_yticks([])
    ax_state.set_ylim(0, 1)
    ax_state.set_title('Mental State', color=TEXT, fontsize=9, pad=4)

    # ── Plot 4: Heart Rate ────────────────────────────────────────────────
    hr_t = [t[i] for i, h in enumerate(hrs) if h is not None]
    hr_v = [h    for h in hrs if h is not None]
    if hr_t:
        ax_hr.plot(hr_t, hr_v, color='#fb7185', lw=1.5, marker='o',
                   markersize=2.5, label='HR (bpm)')
        ax_hr.set_ylim(40, 120)
        ax_hr.set_ylabel('bpm', fontsize=8)
        ax_hr.legend(loc='upper right', fontsize=7.5,
                     facecolor=PANEL, labelcolor=TEXT, framealpha=0.9)
    else:
        ax_hr.text(0.5, 0.5, 'No heart rate data', transform=ax_hr.transAxes,
                   color=DIM, ha='center', va='center', fontsize=9)
    ax_hr.set_title('Heart Rate', color=TEXT, fontsize=9, pad=4)
    ax_hr.set_xlabel('Waktu (detik)', color=DIM, fontsize=8)

    # ── Format x-axis sebagai mm:ss ───────────────────────────────────────
    def fmt_time(x, _):
        m, s = divmod(int(x), 60)
        return f'{m}:{s:02d}'
    ax_hr.xaxis.set_major_formatter(ticker.FuncFormatter(fmt_time))
    ax_hr.xaxis.set_major_locator(ticker.MultipleLocator(30))

    plt.savefig(path.replace('.csv', '.png'), dpi=150, bbox_inches='tight',
                facecolor=BG)
    print(f"💾  Saved: {path.replace('.csv', '.png')}")
    plt.show()


if __name__ == '__main__':
    main()
