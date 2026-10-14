"""English editorial reports: shared data, portable typography, PNG and vector PDF."""

from pathlib import Path
import textwrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import FuncFormatter, MaxNLocator

PAPER, INK, MUTED, GRID, SOFT = "#F8F7F3", "#202D29", "#59675F", "#DFE3DD", "#E8EFE9"
COLORS = {"NL": "#BE6846", "DE": "#266B59"}
FONTS = Path(__file__).parent / "assets/fonts"


def setup():
    for name in ["Lato-Regular.ttf", "Lato-Medium.ttf", "Lato-Bold.ttf"]:
        font_manager.fontManager.addfont(str(FONTS / name))
    plt.rcParams.update(
        {
            "font.family": "Lato",
            "font.size": 10,
            "axes.facecolor": PAPER,
            "figure.facecolor": PAPER,
            "text.color": INK,
            "axes.labelcolor": MUTED,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "axes.edgecolor": GRID,
            "pdf.fonttype": 42,
            "savefig.facecolor": PAPER,
        }
    )


def number(value, decimals=0):
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}".replace(",", " ")


def money(value):
    return "—" if value is None else "€" + number(value)


def signed(value):
    if value is None:
        return "—"
    return ("+" if value > 0 else "−" if value < 0 else "") + money(abs(value))


def compared(analysis):
    return {"NL", "DE"} <= set(analysis["markets"])


def label(analysis):
    return " / ".join(m["name"] for m in analysis["markets"].values())


def text(fig, x, y, content, size=10, *, color=INK, bold=False, align="left"):
    weight = "Lato-Bold.ttf" if bold else "Lato-Regular.ttf"
    return fig.text(
        x,
        y,
        str(content),
        fontsize=size,
        color=color,
        ha=align,
        va="top",
        fontproperties=font_manager.FontProperties(fname=str(FONTS / weight)),
        linespacing=1.35,
    )


def rule(fig, y, left=0.045, right=0.955, color=GRID):
    fig.add_artist(
        plt.Line2D(
            [left, right], [y, y], transform=fig.transFigure, color=color, lw=0.75
        )
    )


def legend(fig, analysis, x, y, *, size=8):
    for index, country in enumerate(analysis["markets"]):
        text(fig, x + index * 0.06, y, "● " + country, size, color=COLORS[country])


def header(fig, analysis, *, detail=False):
    title = analysis["title"]
    if len(title) > 66:
        title = textwrap.fill(title, 66)
    text(
        fig,
        0.045,
        0.990 if not detail else 0.973,
        title,
        13 if detail else 11,
        bold=True,
    )
    text(
        fig,
        0.955,
        0.989 if not detail else 0.971,
        analysis["as_of"][:16].replace("T", " ") + " UTC",
        8,
        color=MUTED,
        align="right",
    )
    if detail:
        text(fig, 0.045, 0.928, label(analysis), 12, color=MUTED)
        rule(fig, 0.902)
    else:
        text(fig, 0.045, 0.964, label(analysis), 38, bold=True)
        text(
            fig,
            0.045,
            0.934,
            "Asking prices and composition of the collected sample",
            13,
            color=MUTED,
        )
        rule(fig, 0.923, color=INK)


def chart(ax, kind, analysis, *, large=False):
    countries = list(analysis["markets"])
    markets = analysis["markets"]
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.6, alpha=0.65)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", length=0, pad=7, labelsize=11 if large else 8)
    sizes = 12 if large else 9
    priced = {
        c: [r for r in m["cars"] if r["price_eur"] is not None]
        for c, m in markets.items()
    }
    if not any(priced.values()) and kind != "gear":
        ax.text(
            0.5,
            0.5,
            "No eligible price data",
            transform=ax.transAxes,
            ha="center",
            color=MUTED,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        return
    missing_dimension = (
        kind in {"years", "age"}
        and not any(r["year"] is not None for rows in priced.values() for r in rows)
    ) or (
        kind == "scatter"
        and not any(
            r["mileage_km"] is not None for rows in priced.values() for r in rows
        )
    )
    if missing_dimension:
        ax.text(
            0.5,
            0.5,
            "No confirmed " + ("mileage" if kind == "scatter" else "year") + " data",
            transform=ax.transAxes,
            ha="center",
            color=MUTED,
        )
        ax.set_xticks([])
        ax.set_yticks([])
        return
    if kind == "years":
        for c in countries:
            stats = markets[c]["year_price_stats"]
            years = sorted(int(y) for y in stats)
            median = [stats[str(y)]["median"] / 1000 for y in years]
            ax.plot(years, median, color=COLORS[c], lw=1.8, marker="o", ms=3)
            ax.fill_between(
                years,
                [stats[str(y)]["q1"] / 1000 for y in years],
                [stats[str(y)]["q3"] / 1000 for y in years],
                color=COLORS[c],
                alpha=0.12,
            )
        ax.set_xlabel("Published vehicle year", fontsize=sizes)
        ax.set_ylabel("Asking price, €k", fontsize=sizes)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=7, integer=True))
    elif kind == "hist":
        values = [r["price_eur"] / 1000 for rows in priced.values() for r in rows]
        bins = np.histogram_bin_edges(
            values, bins=min(12, max(3, int(len(values) ** 0.5)))
        )
        width = (bins[1] - bins[0]) * 0.78 / max(1, len(countries))
        for index, c in enumerate(countries):
            own = [r["price_eur"] / 1000 for r in priced[c]]
            counts, _ = np.histogram(own, bins=bins)
            shares = counts / max(1, len(own)) * 100
            centers = (bins[:-1] + bins[1:]) / 2 + (
                index - (len(countries) - 1) / 2
            ) * width
            ax.bar(centers, shares, width=width * 0.92, color=COLORS[c], zorder=3)
        ax.set_xlabel("Asking price, €k", fontsize=sizes)
        ax.set_ylabel("Share of priced IDs, %", fontsize=sizes)
    elif kind == "scatter":
        for c in countries:
            rows = [r for r in priced[c] if r["mileage_km"] is not None]
            # Dense samples need translucent dots; a small sample needs visible ones.
            scale, alpha = (3, 0.8) if len(rows) < 100 else (1.6, 0.5) if len(rows) < 500 else (1, 0.32)
            ax.scatter(
                [r["mileage_km"] / 1000 for r in rows],
                [r["price_eur"] / 1000 for r in rows],
                color=COLORS[c],
                marker="o" if c == "NL" else "s",
                s=(14 if large else 5) * scale,
                alpha=alpha,
                linewidths=0,
                rasterized=True,
            )
        ax.set_xlabel("Mileage, thousand km", fontsize=sizes)
        ax.set_ylabel("Asking price, €k", fontsize=sizes)
        ax.set_xlim(left=0)
        ax.set_ylim(bottom=0)
    elif kind == "age":
        years = sorted(
            {
                int(r["year"])
                for rows in priced.values()
                for r in rows
                if r["year"] is not None
            }
        )
        width = 0.75 / max(1, len(countries))
        for index, c in enumerate(countries):
            counts = {y: sum(r["year"] == y for r in priced[c]) for y in years}
            ax.bar(
                np.array(years) + (index - (len(countries) - 1) / 2) * width,
                [counts[y] / max(1, len(priced[c])) * 100 for y in years],
                width=width * 0.92,
                color=COLORS[c],
                zorder=3,
            )
        ax.set_xlabel("Published vehicle year", fontsize=sizes)
        ax.set_ylabel("Share of priced IDs, %", fontsize=sizes)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=7, integer=True))
    elif kind == "gear":
        gears = ["automatic", "manual", "unknown"]
        width = 0.7 / max(1, len(countries))
        for index, c in enumerate(countries):
            market = markets[c]
            nums = [market["gear_counts"].get(g, 0) for g in gears]
            shares = [n / max(1, market["live_ids"]) * 100 for n in nums]
            bars = ax.bar(
                np.arange(3) + (index - (len(countries) - 1) / 2) * width,
                shares,
                width=width * 0.92,
                color=COLORS[c],
                zorder=3,
            )
            ax.bar_label(
                bars,
                labels=[f"{p:.1f}%\n{n}" for p, n in zip(shares, nums)],
                padding=4,
                fontsize=10 if large else 7,
                color=COLORS[c],
            )
        ax.set_xticks(range(3), ["Automatic", "Manual", "Unknown"])
        ax.set_ylabel("Share of catalog IDs, %", fontsize=sizes)
        ax.set_ylim(0, 116)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))


TITLES = {
    "years": "Prices by year",
    "hist": "Price distribution",
    "scatter": "Price and mileage",
    "age": "Sample composition by year",
    "gear": "Transmissions",
}


def caption(kind, analysis):
    markets = analysis["markets"]
    if kind == "years":
        return (
            "Median and interquartile range; all transmissions and declared conditions."
        )
    if kind == "hist":
        return "One eligible minimum full purchase quote per catalog ID."
    if kind == "scatter":
        return "Known mileage: " + " / ".join(
            f"{c} {m['priced_ids'] - m['unknown_mileage_ids']} IDs"
            for c, m in markets.items()
        )
    if kind == "age":
        return "Unknown year: " + " / ".join(
            f"{c} {m['unknown_year_ids']} priced IDs" for c, m in markets.items()
        )
    return "Shares and counts include IDs without an eligible price."


def panel(fig, analysis, kind, x, top, *, width=0.43, height=0.115):
    text(fig, x, top, TITLES[kind], 13, bold=True)
    legend(fig, analysis, x + width - 0.115, top, size=7)
    text(fig, x, top - 0.018, caption(kind, analysis), 8, color=MUTED)
    ax = fig.add_axes([x + 0.035, top - height - 0.043, width - 0.044, height])
    chart(ax, kind, analysis)


def focus(fig, analysis):
    fig.add_artist(
        FancyBboxPatch(
            (0.045, 0.710),
            0.91,
            0.099,
            boxstyle="round,pad=0.006,rounding_size=0.007",
            transform=fig.transFigure,
            fc=SOFT,
            ec="none",
            zorder=0,
        )
    )
    groups = analysis["selected_cohorts"]
    if not groups:
        text(
            fig,
            0.06,
            0.786,
            "No comparable cohort meets the sample threshold",
            15,
            bold=True,
        )
        text(
            fig,
            0.06,
            0.752,
            f"At least {analysis['policy']['minimum_cohort_size']} priced IDs are required per market in the same year, transmission and mileage band.",
            10,
            color=MUTED,
        )
        text(
            fig,
            0.06,
            0.729,
            "The report keeps missing data and small samples visible; no cross-market difference is invented.",
            9,
            color=MUTED,
        )
        return
    group = groups[0]
    text(fig, 0.06, 0.795, group["label"], 15, bold=True)
    countries = list(analysis["markets"])
    for index, c in enumerate(countries):
        x = 0.06 + index * 0.34
        stats = group["markets"][c]
        text(fig, x, 0.766, analysis["markets"][c]["name"], 9, color=COLORS[c])
        text(fig, x, 0.746, money(stats["median"]), 31, color=COLORS[c], bold=True)
        text(
            fig,
            x,
            0.719,
            f"n = {stats['n']}  ·  middle 50% {money(stats['q1'])}–{money(stats['q3'])}",
            8,
            color=MUTED,
        )
    if "delta_eur" in group:
        text(fig, 0.77, 0.766, "Median difference DE − NL", 8, color=MUTED)
        text(fig, 0.77, 0.746, signed(group["delta_eur"]), 24, bold=True)
        text(fig, 0.77, 0.722, f"{group['delta_pct']:+.1f}%", 17, color=COLORS["DE"])
    text(
        fig,
        0.06,
        0.701,
        "Most supported cohort. Generation, trim and condition are not controlled"
        + ("; Germany before import costs." if compared(analysis) else "."),
        7.5,
        color=MUTED,
    )


def cohort_table(fig, analysis, *, detail=False):
    rows = analysis["selected_cohorts"]
    countries = list(analysis["markets"])
    top = 0.825 if detail else 0.346
    text(
        fig, 0.045, top, "Prices in comparable groups", 20 if detail else 15, bold=True
    )
    text(
        fig,
        0.045,
        top - (0.036 if detail else 0.02),
        "No declared current damage; condition unverified. Selection is by sample support, not discount.",
        10 if detail else 8,
        color=MUTED,
    )
    if not rows:
        text(
            fig,
            0.045,
            top - 0.1,
            "Insufficient comparable data",
            18 if detail else 13,
            color=MUTED,
        )
        return
    if detail:
        y = 0.72
        for group in rows:
            text(fig, 0.045, y, group["label"], 16, bold=True)
            for index, c in enumerate(countries):
                x = 0.045 + index * 0.46
                stats = group["markets"][c]
                text(
                    fig,
                    x,
                    y - 0.034,
                    c + "  " + money(stats["median"]),
                    24,
                    color=COLORS[c],
                    bold=True,
                )
                text(
                    fig,
                    x,
                    y - 0.077,
                    f"n = {stats['n']}  ·  middle 50% {money(stats['q1'])}–{money(stats['q3'])}",
                    11,
                    color=MUTED,
                )
            rule(fig, y - 0.107)
            y -= 0.125
        return
    y = 0.305
    text(fig, 0.045, y, "Year / transmission / mileage", 8, color=MUTED)
    for index, c in enumerate(countries):
        text(fig, 0.55 + index * 0.2, y, c + " median", 8, color=MUTED, align="right")
        text(fig, 0.60 + index * 0.2, y, "n", 8, color=MUTED, align="right")
    if len(countries) == 2:
        text(fig, 0.955, y, "DE − NL", 8, color=MUTED, align="right")
    rule(fig, y - 0.013, color=INK)
    y -= 0.026
    for group in rows:
        text(fig, 0.045, y, str(group["year"]), 12, bold=True)
        text(fig, 0.14, y, group["transmission"].capitalize(), 10)
        text(
            fig,
            0.14,
            y - 0.012,
            f"{group['mileage_low'] / 1000:g}–{group['mileage_high'] / 1000:g}k km",
            8,
            color=MUTED,
        )
        for index, c in enumerate(countries):
            stats = group["markets"][c]
            text(
                fig,
                0.55 + index * 0.2,
                y,
                money(stats["median"]),
                12,
                color=COLORS[c],
                bold=True,
                align="right",
            )
            text(fig, 0.60 + index * 0.2, y, stats["n"], 9, color=MUTED, align="right")
        if "delta_eur" in group:
            text(
                fig,
                0.955,
                y,
                signed(group["delta_eur"]),
                11,
                color=COLORS["DE"],
                align="right",
            )
        rule(fig, y - 0.022)
        y -= 0.024


def summary(analysis, output):
    fig = plt.figure(figsize=(14, 23.4), dpi=200)
    header(fig, analysis)
    countries = list(analysis["markets"])
    for index, c in enumerate(countries):
        x = 0.045 + index * 0.485
        m = analysis["markets"][c]
        text(fig, x, 0.909, "● " + m["name"], 14, color=COLORS[c], bold=True)
        text(fig, x, 0.889, "Overall median asking price", 10, color=MUTED)
        text(fig, x, 0.875, money(m["price_stats"]["median"]), 40, bold=True)
        text(
            fig,
            x,
            0.84,
            f"{number(m['priced_ids'])} prices  /  {number(m['live_ids'])} catalog IDs",
            11,
        )
        text(
            fig,
            x,
            0.825,
            "Median year "
            + (
                "—"
                if m["year_stats"]["median"] is None
                else f"{m['year_stats']['median']:.1f}"
                if m["year_stats"]["median"] % 1
                else f"{m['year_stats']['median']:.0f}"
            )
            + "  ·  mileage "
            + (
                number(m["mileage_stats"]["median"] / 1000, 1) + "k km"
                if m["mileage_stats"]["median"] is not None
                else "unknown"
            ),
            9,
            color=MUTED,
        )
    focus(fig, analysis)
    panel(fig, analysis, "years", 0.045, 0.676, height=0.075)
    panel(fig, analysis, "hist", 0.53, 0.676, height=0.075)
    panel(fig, analysis, "scatter", 0.045, 0.519, height=0.075)
    panel(fig, analysis, "age", 0.53, 0.519, height=0.075)
    cohort_table(fig, analysis)
    panel(fig, analysis, "gear", 0.045, 0.144, height=0.046)
    text(fig, 0.53, 0.144, "Collected data", 13, bold=True)
    text(
        fig,
        0.53,
        0.125,
        "Publication counts and catalog identities are different measures.",
        8,
        color=MUTED,
    )
    for index, c in enumerate(countries):
        text(fig, 0.80 + index * 0.15, 0.105, c, 8, color=COLORS[c], align="right")
    for row_index, (key, title) in enumerate(
        [
            ("all_publications", "All stored publications"),
            ("live_target_publications", "Eligible observed publications"),
            ("known_identifier_ids", "IDs with a strong identifier"),
            ("provisional_ids", "Provisional catalog IDs"),
            ("unpriced_ids", "IDs without an eligible price"),
        ]
    ):
        y = 0.093 - row_index * 0.011
        text(fig, 0.53, y, title, 8, color=MUTED)
        for index, c in enumerate(countries):
            text(
                fig,
                0.80 + index * 0.15,
                y,
                number(analysis["markets"][c][key]),
                10,
                color=COLORS[c],
                bold=True,
                align="right",
            )
    rule(fig, 0.02, color=INK)
    text(
        fig,
        0.045,
        0.015,
        "Collected sample, not the whole market. Catalog IDs may include duplicates. Asking prices are not transaction prices.",
        7.5,
        color=MUTED,
    )
    text(
        fig,
        0.045,
        0.007,
        "Generation, trim and condition are not controlled. "
        + ("Germany before import costs. " if compared(analysis) else "")
        + "Source dates and exclusions are retained in the data package.",
        7.5,
        color=MUTED,
    )
    fig.savefig(output / "comparison-editorial.png", dpi=200)
    fig.savefig(output / "comparison-editorial.pdf")
    plt.close(fig)


def details(analysis, output):
    with PdfPages(output / "comparison-editorial-details.pdf") as pdf:
        for kind in ["years", "hist", "scatter", "age", "gear"]:
            fig = plt.figure(figsize=(11.7, 14.5), dpi=150)
            header(fig, analysis, detail=True)
            text(fig, 0.045, 0.854, TITLES[kind], 25, bold=True)
            text(fig, 0.045, 0.81, caption(kind, analysis), 12, color=MUTED)
            legend(fig, analysis, 0.80, 0.85, size=10)
            height, bottom = (0.39, 0.35) if kind == "years" else (0.53, 0.23)
            ax = fig.add_axes([0.105, bottom, 0.84, height])
            chart(ax, kind, analysis, large=True)
            if kind == "years":
                years = sorted(
                    {
                        int(y)
                        for m in analysis["markets"].values()
                        for y in m["year_price_stats"]
                    }
                )
                shown = years[-14:]
                y = 0.29
                text(fig, 0.045, y, "Year", 10, color=MUTED)
                for index, c in enumerate(analysis["markets"]):
                    text(
                        fig,
                        0.45 + index * 0.43,
                        y,
                        c + " median / n",
                        10,
                        color=COLORS[c],
                        align="right",
                    )
                step = min(0.014, 0.20 / max(1, len(shown)))
                for year in shown:
                    y -= step
                    text(fig, 0.045, y, year, 9)
                    for index, (c, m) in enumerate(analysis["markets"].items()):
                        stats = m["year_price_stats"].get(str(year), {})
                        text(
                            fig,
                            0.45 + index * 0.43,
                            y,
                            money(stats.get("median")) + " / " + str(stats.get("n", 0)),
                            9,
                            color=COLORS[c],
                            align="right",
                        )
                if len(years) > len(shown):
                    text(
                        fig,
                        0.045,
                        0.072,
                        "Latest 14 years shown in this table; all years are included in the chart and JSON.",
                        8,
                        color=MUTED,
                    )
            text(
                fig,
                0.045,
                0.054,
                "Collected sample. Completeness and unique physical vehicle counts are not proven.",
                9,
                color=MUTED,
            )
            pdf.savefig(fig)
            fig.savefig(output / ("detail-" + kind + ".png"), dpi=150)
            plt.close(fig)
        fig = plt.figure(figsize=(11.7, 16.5), dpi=150)
        header(fig, analysis, detail=True)
        cohort_table(fig, analysis, detail=True)
        text(
            fig,
            0.045,
            0.07,
            "Same year, transmission and mileage band; no declared current damage. Condition, generation and trim remain uncontrolled.",
            9,
            color=MUTED,
        )
        text(
            fig,
            0.045,
            0.048,
            ("Germany before import costs. " if compared(analysis) else "")
            + "A difference in asking prices is not a guaranteed saving or future resale result.",
            9,
            color=MUTED,
        )
        pdf.savefig(fig)
        fig.savefig(output / "detail-cohorts.png", dpi=150)
        plt.close(fig)
        for c, m in analysis["markets"].items():
            counts = m["all_source_counts"]
            dealers = sum(n for s, n in counts.items() if s.startswith("dealer:"))
            rows = [
                (s.removesuffix("_de"), n)
                for s, n in counts.items()
                if not s.startswith("dealer:")
            ]
            if dealers:
                rows.append(("Direct dealer websites", dealers))
            rows.sort(key=lambda row: (-row[1], row[0]))
            # Any number of sources is paginated rather than clipped or silently dropped.
            chunks = [rows[i : i + 22] for i in range(0, len(rows), 22)] or [[]]
            for page_index, chunk in enumerate(chunks):
                fig = plt.figure(figsize=(11.7, 14.5), dpi=150)
                header(fig, analysis, detail=True)
                text(fig, 0.045, 0.851, m["name"] + " · sources", 25, bold=True)
                text(
                    fig,
                    0.045,
                    0.807,
                    f"{number(m['all_publications'])} stored publications, including other scopes and history",
                    12,
                    color=MUTED,
                )
                if len(chunks) > 1:
                    text(
                        fig,
                        0.955,
                        0.85,
                        f"{page_index + 1}/{len(chunks)}",
                        11,
                        color=MUTED,
                        align="right",
                    )
                maximum = max((n for _, n in rows), default=1)
                y = 0.743
                step = min(0.028, 0.62 / max(1, len(chunk)))
                for source, count in chunk:
                    text(
                        fig,
                        0.045,
                        y,
                        source if len(source) <= 46 else source[:43] + "…",
                        11,
                    )
                    fig.add_artist(
                        plt.Rectangle(
                            (0.41, y - 0.014),
                            0.43 * count / maximum,
                            0.01,
                            transform=fig.transFigure,
                            color=COLORS[c],
                            lw=0,
                        )
                    )
                    text(
                        fig,
                        0.955,
                        y,
                        number(count),
                        13,
                        color=COLORS[c],
                        bold=True,
                        align="right",
                    )
                    y -= step
                rule(fig, 0.095)
                text(
                    fig,
                    0.045,
                    0.076,
                    "Publication volume is not the number of cars or evidence of complete country/source coverage.",
                    10,
                    color=MUTED,
                )
                pdf.savefig(fig)
                fig.savefig(
                    output
                    / (
                        "detail-sources-"
                        + c.lower()
                        + ("-" + str(page_index + 1) if len(chunks) > 1 else "")
                        + ".png"
                    ),
                    dpi=150,
                )
                plt.close(fig)


def render(analysis, output):
    setup()
    output = Path(output)
    summary(analysis, output)
    details(analysis, output)
