// Imperative chart engine behind <MainChart/>. Owns the lightweight-charts instance, the series, the
// incremental aggregators and the built-in primitives, and keeps them in sync with the zustand store.
//
// Data flow per animation frame (at most once, coalesced through rAF):
//   store.series.{bars,odds,fair,ticks} --BucketAggregator.sync(tf)--> "none" | "full" | "tail"
//   full -> series.setData(all)   (timeframe change or a history load swapped the array identity)
//   tail -> series.update(item)   for the re-folded last bucket + any new buckets (O(1) per frame)

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LastPriceAnimationMode,
  LineSeries,
  LineStyle,
  createChart,
  type AutoscaleInfo,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesPrimitive,
  type LineData,
  type Logical,
  type MouseEventParams,
  type PriceFormatCustom,
  type SeriesType,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import type { ExchangeId } from "@shared/protocol";
import { serverNow, useTerminal, type TerminalState } from "@/lib/store";
import {
  findAt,
  indexAtOrBefore,
  makeBarAgg,
  makeFairAgg,
  makeOddsAgg,
  makeTickAgg,
  type Bucketed,
  type CandleAgg,
  type FairAgg,
  type OddsAgg,
  type SyncResult,
  type TickAgg,
} from "./aggregate";
import type { ChartHandle, ScaleKey } from "./context";
import { fmtBtc, fmtCents, fmtSignedCents, fmtSignedUsd } from "./fmt";
import { NowLinePrimitive } from "./primitives/NowLine";
import { OddsBandPrimitive } from "./primitives/OddsBand";
import { PaneLabelPrimitive } from "./primitives/PaneLabel";
import { PriceToBeatPrimitive } from "./primitives/PriceToBeat";
import { WindowBandsPrimitive } from "./primitives/WindowBands";
import { C, fonts, resolveMonoFont } from "./theme";
import { crosshairTimeFormatter, hmAp, hms, mss, tickMarkFormatter } from "./time";
import { Tooltip } from "./tooltip";
import { windowAt, windowList } from "./windows";

const TRANSPARENT = "rgba(0,0,0,0)";
const EXCHANGES: ExchangeId[] = ["coinbase", "kraken", "bitstamp"];
const RIGHT_PAD_BARS = 4;
const EDGE_RANGE = 10; // ±10¢ fixed edge axis
const EDGE_STRETCH = 0.24;

const btcFormat: PriceFormatCustom = { type: "custom", minMove: 0.01, formatter: (p) => fmtBtc(p) };
const centsFormat: PriceFormatCustom = {
  type: "custom",
  minMove: 0.1,
  formatter: (p) => fmtCents(p),
  tickmarksFormatter: (ps) => ps.map((p) => `${Math.round(p)}¢`),
};
const edgeFormat: PriceFormatCustom = {
  type: "custom",
  minMove: 0.1,
  formatter: (p) => fmtSignedCents(p),
  tickmarksFormatter: (ps) => ps.map((p) => (Math.abs(p) < 1e-9 ? "0¢" : `${p > 0 ? "+" : "−"}${Math.abs(Math.round(p))}¢`)),
};
const fixedCents = (): AutoscaleInfo => ({ priceRange: { minValue: 0, maxValue: 100 } });
const fixedEdge = (): AutoscaleInfo => ({ priceRange: { minValue: -EDGE_RANGE, maxValue: EDGE_RANGE } });

type LinePoint = LineData<Time> | WhitespaceData<Time>;
const T = (sec: number) => sec as UTCTimestamp;

/** Default amount of history shown left of "now", per timeframe (seconds). */
const HISTORY_SPAN: Record<number, number> = { 1: 300, 5: 1500, 15: 1800, 60: 4 * 3600, 300: 16 * 3600 };

export class ChartEngine implements ChartHandle {
  readonly chart: IChartApi;
  readonly container: HTMLElement;
  readonly btcSeries: ISeriesApi<"Line">;
  readonly oddsSeries: ISeriesApi<"Line">;

  private readonly candles: ISeriesApi<"Candlestick">;
  private readonly downLine: ISeriesApi<"Line">;
  private readonly fairLine: ISeriesApi<"Line">;
  private readonly exLines: Record<ExchangeId, ISeriesApi<"Line">>;
  /** hidden whitespace series that extends the time scale to the current window's end */
  private readonly timeline: ISeriesApi<"Line">;
  private edge: ISeriesApi<"Histogram"> | null = null;
  private edgeZero: IPriceLine | null = null;

  private readonly aggs = { bars: makeBarAgg(), odds: makeOddsAgg(), fair: makeFairAgg(), ticks: makeTickAgg() };
  private readonly bands: WindowBandsPrimitive;
  private readonly band: OddsBandPrimitive;
  private readonly ptb: PriceToBeatPrimitive;
  private readonly now: NowLinePrimitive;
  private readonly edgeLabel: PaneLabelPrimitive;
  private readonly tooltip: Tooltip;

  private tf = 0;
  private lineMode = false;
  private timelineKey = "";
  private timelineWinId: string | null = null;
  private needsDefaultView = true;
  private hoverTime: number | null = null;
  private hoverEdge: number | null = null;
  private raf = 0;
  private readonly timer: ReturnType<typeof setInterval>;
  private readonly unsub: () => void;
  private destroyed = false;

  constructor(host: HTMLElement, chartEl: HTMLElement) {
    this.container = host;
    fonts.mono = resolveMonoFont(host);
    const st = useTerminal.getState();

    this.chart = createChart(chartEl, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: C.bg },
        textColor: C.label,
        fontSize: 11,
        fontFamily: fonts.mono,
        panes: { separatorColor: C.border, separatorHoverColor: "rgba(91, 156, 255, 0.18)", enableResize: true },
      },
      grid: {
        vertLines: { color: C.grid },
        horzLines: { color: C.grid },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "rgba(163, 171, 185, 0.4)", style: LineStyle.LargeDashed, width: 1, labelBackgroundColor: C.borderStrong },
        horzLine: { color: "rgba(163, 171, 185, 0.4)", style: LineStyle.LargeDashed, width: 1, labelBackgroundColor: C.borderStrong },
      },
      rightPriceScale: {
        visible: true,
        borderColor: C.border,
        scaleMargins: { top: 0.1, bottom: 0.14 },
        minimumWidth: 76,
      },
      leftPriceScale: {
        visible: true,
        borderColor: C.border,
        scaleMargins: { top: 0.06, bottom: 0.1 },
        minimumWidth: 44,
      },
      timeScale: {
        borderColor: C.border,
        timeVisible: true,
        secondsVisible: st.timeframe < 60,
        rightOffset: RIGHT_PAD_BARS,
        barSpacing: 8,
        minBarSpacing: 0.25,
        tickMarkFormatter,
      },
      localization: { locale: "en-US", timeFormatter: crosshairTimeFormatter },
    });

    const noMarkers = { lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false } as const;

    // --- right scale: BTC
    this.candles = this.chart.addSeries(CandlestickSeries, {
      priceScaleId: "right",
      upColor: C.up,
      downColor: C.down,
      borderUpColor: C.up,
      borderDownColor: C.down,
      wickUpColor: C.up,
      wickDownColor: C.down,
      priceFormat: btcFormat,
      priceLineStyle: LineStyle.Dotted,
      priceLineWidth: 1,
    });
    this.exLines = {} as Record<ExchangeId, ISeriesApi<"Line">>;
    for (const ex of EXCHANGES) {
      this.exLines[ex] = this.chart.addSeries(LineSeries, {
        ...noMarkers,
        priceScaleId: "right",
        color: C.exchange[ex],
        lineWidth: 1,
        priceFormat: btcFormat,
        visible: false,
        title: "",
      });
    }
    // anchor for right-scale primitives; doubles as the BTC series in line mode
    this.btcSeries = this.chart.addSeries(LineSeries, {
      ...noMarkers,
      priceScaleId: "right",
      color: C.btcLine,
      lineWidth: 2,
      lineVisible: false,
      priceFormat: btcFormat,
      priceLineStyle: LineStyle.Dotted,
      lastPriceAnimation: LastPriceAnimationMode.Disabled,
    });

    // --- left scale: odds (0–100¢)
    const leftCommon = { priceScaleId: "left", priceFormat: centsFormat, autoscaleInfoProvider: fixedCents } as const;
    this.fairLine = this.chart.addSeries(LineSeries, {
      ...noMarkers,
      ...leftCommon,
      color: C.fair,
      lineWidth: 2,
      lineStyle: LineStyle.Dashed,
    });
    this.downLine = this.chart.addSeries(LineSeries, {
      ...noMarkers,
      ...leftCommon,
      color: C.downOdds,
      lineWidth: 1,
      visible: false,
    });
    this.oddsSeries = this.chart.addSeries(LineSeries, {
      ...leftCommon,
      color: C.odds,
      lineWidth: 2,
      lastValueVisible: true,
      priceLineVisible: false,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 3,
      crosshairMarkerBorderColor: C.bg,
      crosshairMarkerBackgroundColor: C.odds,
    });

    // --- time-scale extender (own invisible overlay scale so it never affects autoscale)
    this.timeline = this.chart.addSeries(LineSeries, { ...noMarkers, priceScaleId: "timeline", lineVisible: false });

    // --- primitives
    this.bands = new WindowBandsPrimitive(this);
    this.ptb = new PriceToBeatPrimitive(this);
    this.now = new NowLinePrimitive(this);
    this.band = new OddsBandPrimitive(this);
    this.edgeLabel = new PaneLabelPrimitive(this, "EDGE · FAIR − MARKET");
    this.bands.getReserved = () => (this.now.isEnabled() ? this.now.reserved : null);
    this.btcSeries.attachPrimitive(this.bands);
    this.btcSeries.attachPrimitive(this.ptb);
    this.btcSeries.attachPrimitive(this.now);
    this.oddsSeries.attachPrimitive(this.band);

    this.tooltip = new Tooltip(host);
    this.chart.subscribeCrosshairMove(this.onCrosshair);
    this.chart.timeScale().subscribeSizeChange(this.onSize);

    this.applyStyle();
    this.applyOverlays();
    this.sync();

    this.unsub = useTerminal.subscribe(this.onStore);
    this.timer = setInterval(this.onTick, 500);

    // canvas text can only use the real web font once it has loaded
    if (typeof document !== "undefined" && document.fonts?.ready) {
      document.fonts.ready.then(() => {
        if (this.destroyed) return;
        fonts.mono = resolveMonoFont(host);
        this.chart.applyOptions({ layout: { fontFamily: fonts.mono } });
      });
    }
  }

  destroy() {
    this.destroyed = true;
    this.unsub();
    clearInterval(this.timer);
    if (this.raf) cancelAnimationFrame(this.raf);
    this.chart.unsubscribeCrosshairMove(this.onCrosshair);
    this.chart.timeScale().unsubscribeSizeChange(this.onSize);
    this.tooltip.destroy();
    this.chart.remove();
  }

  // ---- ChartHandle --------------------------------------------------------------------------------

  timeframe() {
    return this.tf;
  }

  private barSpacing(): number {
    const ts = this.chart.timeScale();
    const a = ts.logicalToCoordinate(0 as Logical);
    const b = ts.logicalToCoordinate(1 as Logical);
    return a != null && b != null ? b - a : 6;
  }

  timeToX(ms: number): number | null {
    const tf = this.tf || 1;
    const ts = this.chart.timeScale();
    const sec = ms / 1000;
    const b = Math.floor(sec / tf) * tf;
    const spacing = this.barSpacing();
    const xb = ts.timeToCoordinate(T(b));
    // a bucket [b, b+tf) is drawn centred on x(b): map time linearly across the bar's width
    if (xb != null) return xb + ((sec - b) / tf - 0.5) * spacing;
    const arr = this.aggs.bars.out;
    if (!arr.length) return null;
    const i = indexAtOrBefore(arr, b);
    if (i < 0) {
      const x0 = ts.timeToCoordinate(T(arr[0].time));
      return x0 == null ? null : x0 - ((arr[0].time - sec) / tf + 0.5) * spacing;
    }
    const a = arr[i];
    const xa = ts.timeToCoordinate(T(a.time));
    if (xa == null) return null;
    const n = arr[i + 1];
    if (n) {
      const xn = ts.timeToCoordinate(T(n.time));
      if (xn != null) return xa - spacing / 2 + ((sec - a.time) / (n.time - a.time)) * (xn - xa);
    }
    return xa + ((sec - a.time) / tf - 0.5) * spacing;
  }

  xToTime(x: number): number | null {
    const ts = this.chart.timeScale();
    const t = ts.coordinateToTime(x);
    if (t == null) return null;
    const xc = ts.timeToCoordinate(t);
    if (xc == null) return (t as number) * 1000;
    const tf = this.tf || 1;
    return ((t as number) + ((x - xc) / this.barSpacing() + 0.5) * tf) * 1000;
  }

  btcToY(price: number) {
    return this.btcSeries.priceToCoordinate(price);
  }
  yToBtc(y: number) {
    return this.btcSeries.coordinateToPrice(y);
  }
  centsToY(c: number) {
    return this.oddsSeries.priceToCoordinate(c);
  }
  yToCents(y: number) {
    return this.oddsSeries.coordinateToPrice(y);
  }

  attach(p: ISeriesPrimitive<Time>, scale: ScaleKey): () => void {
    const s = scale === "btc" ? this.btcSeries : this.oddsSeries;
    s.attachPrimitive(p);
    return () => {
      if (!this.destroyed) s.detachPrimitive(p);
    };
  }

  data() {
    return { candles: this.aggs.bars.out, odds: this.aggs.odds.out, fair: this.aggs.fair.out };
  }

  requestRedraw() {
    this.now.update();
  }

  // ---- store wiring -------------------------------------------------------------------------------

  private onStore = (s: TerminalState, p: TerminalState) => {
    if (s.chartStyle !== p.chartStyle) this.applyStyle();
    if (s.overlays !== p.overlays) this.applyOverlays();
    if (
      s.version !== p.version ||
      s.series !== p.series ||
      s.timeframe !== p.timeframe ||
      s.current !== p.current ||
      s.windows !== p.windows ||
      s.recent !== p.recent
    )
      this.schedule();
  };

  private schedule() {
    if (this.raf || this.destroyed) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      if (!this.destroyed) this.sync();
    });
  }

  private onTick = () => {
    this.now.update();
  };

  private onSize = () => {
    if (this.needsDefaultView) this.applyDefaultView();
  };

  // ---- data sync ----------------------------------------------------------------------------------

  private sync() {
    const st = useTerminal.getState();
    const tf = st.timeframe;
    if (tf !== this.tf) {
      this.tf = tf;
      this.needsDefaultView = true;
      this.chart.applyOptions({ timeScale: { secondsVisible: tf < 60 } });
    }
    const { bars, odds, fair, ticks } = st.series;
    const a = this.aggs;
    // a history load replaces array identities -> the aggregators rebuild and we re-frame the view
    const rb = a.bars.sync(bars, tf);
    if (rb.kind === "full") this.needsDefaultView = true;
    this.apply(rb, a.bars.out, this.candles, candlePoint);
    this.apply(rb, a.bars.out, this.btcSeries, this.btcLinePoint);

    const ro = a.odds.sync(odds, tf);
    this.apply(ro, a.odds.out, this.oddsSeries, (o, i, out) => breakLine(o, out[i + 1], o.upMid));
    this.apply(ro, a.odds.out, this.downLine, (o, i, out) => breakLine(o, out[i + 1], o.downMid));

    const rf = a.fair.sync(fair, tf);
    this.apply(rf, a.fair.out, this.fairLine, (f, i, out) => breakLine(f, out[i + 1], f.fair));
    if (this.edge) this.apply(rf, a.fair.out, this.edge, edgePoint);

    const rt = a.ticks.sync(ticks, tf);
    for (const ex of EXCHANGES) this.apply(rt, a.ticks.out, this.exLines[ex], (t) => exPoint(t, ex));

    this.updateTimeline(st, tf);
    if (this.needsDefaultView) this.applyDefaultView();
    this.updateEdgeLabel();
  }

  private apply<O extends Bucketed, S extends SeriesType>(
    r: SyncResult,
    out: readonly O[],
    series: ISeriesApi<S>,
    map: (o: O, i: number, out: readonly O[]) => unknown,
  ) {
    type D = Parameters<ISeriesApi<S>["update"]>[0];
    if (r.kind === "none") return;
    if (r.kind === "full") {
      series.setData(out.map((o, i) => map(o, i, out)) as D[]);
      return;
    }
    try {
      for (let i = r.from; i < out.length; i++) series.update(map(out[i], i, out) as D);
    } catch {
      // out-of-order update (should not happen) — fall back to a full reset of this series
      series.setData(out.map((o, i) => map(o, i, out)) as D[]);
    }
  }

  private btcLinePoint = (c: CandleAgg, i: number, out: readonly CandleAgg[]): LineData<Time> => {
    // the last point carries the candle direction so the right-axis tag is green/red in line mode
    if (this.lineMode && i === out.length - 1)
      return { time: T(c.time), value: c.close, color: c.close >= c.open ? C.up : C.down };
    return { time: T(c.time), value: c.close };
  };

  /** Extend the time scale to the current window's end with whitespace so "now" has empty space after it. */
  private updateTimeline(st: TerminalState, tf: number) {
    const cur = st.current;
    const key = cur ? `${cur.id}|${cur.startTs}|${cur.endTs}|${tf}` : `none|${tf}`;
    if (key === this.timelineKey) return;
    const ts = this.chart.timeScale();
    // follow the live edge across a rollover if the user was looking at it
    let follow = false;
    if (cur && this.timelineWinId && this.timelineWinId !== cur.id) {
      const prevEnd = st.windows[this.timelineWinId]?.endTs ?? cur.startTs;
      const vr = ts.getVisibleRange();
      follow = !vr || (vr.to as number) >= prevEnd / 1000 - tf * 2;
    }
    this.timelineKey = key;
    this.timelineWinId = cur?.id ?? null;
    if (!cur) {
      this.timeline.setData([]);
      return;
    }
    const pts: WhitespaceData<Time>[] = [];
    const end = cur.endTs / 1000;
    for (let t = Math.floor(cur.startTs / 1000 / tf) * tf; t <= end; t += tf) pts.push({ time: T(t) });
    this.timeline.setData(pts);
    if (follow) this.needsDefaultView = true;
  }

  /** Frame recent history + the rest of the current window (empty space until the window ends). */
  private applyDefaultView() {
    const ts = this.chart.timeScale();
    const bars = this.aggs.bars.out;
    if (ts.width() <= 0 || !bars.length) return;
    const st = useTerminal.getState();
    const tf = this.tf;
    const nowS = serverNow() / 1000;
    const cur = st.current;
    const last = bars[bars.length - 1].time;
    const end = cur ? cur.endTs / 1000 : last + tf * 20;
    let from = Math.min(nowS, last) - (HISTORY_SPAN[tf] ?? tf * 120);
    if (tf === 15 && cur) from = Math.min(from, cur.startTs / 1000 - 900);
    from = Math.max(from, bars[0].time);
    try {
      ts.setVisibleRange({ from: T(from), to: T(end) });
      const lr = ts.getVisibleLogicalRange();
      if (lr) ts.setVisibleLogicalRange({ from: lr.from, to: lr.to + RIGHT_PAD_BARS });
      this.needsDefaultView = false;
    } catch {
      /* time scale not ready yet; retry on the next sync/size change */
    }
  }

  // ---- overlays / style -------------------------------------------------------------------------

  private applyStyle() {
    const line = useTerminal.getState().chartStyle === "line";
    this.lineMode = line;
    this.candles.applyOptions({ visible: !line });
    this.btcSeries.applyOptions({
      lineVisible: line,
      lastValueVisible: line,
      priceLineVisible: line,
      crosshairMarkerVisible: line,
    });
    // re-emit the last point so its direction color is set/cleared
    const out = this.aggs.bars.out;
    if (out.length) this.btcSeries.update(this.btcLinePoint(out[out.length - 1], out.length - 1, out));
  }

  private applyOverlays() {
    const ov = useTerminal.getState().overlays;
    this.oddsSeries.applyOptions({
      lineVisible: ov.upOdds,
      lastValueVisible: ov.upOdds,
      crosshairMarkerVisible: ov.upOdds,
    });
    this.band.setEnabled(ov.upOdds);
    this.downLine.applyOptions({ visible: ov.downOdds });
    this.fairLine.applyOptions({ visible: ov.fair });
    this.ptb.setEnabled(ov.ptb);
    this.bands.setEnabled(ov.windows);
    for (const ex of EXCHANGES) this.exLines[ex].applyOptions({ visible: ov.perExchange });
    this.setEdgePane(ov.edgePane);
  }

  private setEdgePane(on: boolean) {
    if (on && !this.edge) {
      const edge = this.chart.addSeries(
        HistogramSeries,
        {
          priceScaleId: "right",
          priceFormat: edgeFormat,
          autoscaleInfoProvider: fixedEdge,
          priceLineVisible: false,
          lastValueVisible: true,
          base: 0,
        },
        1,
      );
      edge.priceScale().applyOptions({ scaleMargins: { top: 0.18, bottom: 0.08 }, borderColor: C.border, minimumWidth: 76 });
      this.edgeZero = edge.createPriceLine({
        price: 0,
        color: C.borderStrong,
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: false,
        title: "",
      });
      edge.attachPrimitive(this.edgeLabel);
      edge.setData(this.aggs.fair.out.map(edgePoint));
      this.edge = edge;
      const panes = this.chart.panes();
      panes[0]?.setStretchFactor(1);
      panes[1]?.setStretchFactor(EDGE_STRETCH);
      this.updateEdgeLabel();
    } else if (!on && this.edge) {
      this.edge.detachPrimitive(this.edgeLabel);
      if (this.edgeZero) this.edge.removePriceLine(this.edgeZero);
      this.chart.removeSeries(this.edge);
      this.edge = null;
      this.edgeZero = null;
    }
  }

  private updateEdgeLabel() {
    if (!this.edge) return;
    let v = this.hoverEdge;
    if (v == null) {
      const f = this.aggs.fair.out;
      for (let i = f.length - 1; i >= 0 && i >= f.length - 3; i--) {
        if (f[i].edge != null) {
          v = f[i].edge;
          break;
        }
      }
    }
    this.edgeLabel.value = v == null ? null : { text: fmtSignedCents(v), color: v >= 0 ? C.oddsText : C.negEdge };
  }

  // ---- crosshair: tooltip + store hover ---------------------------------------------------------

  private onCrosshair = (p: MouseEventParams<Time>) => {
    if (!p.point || p.time == null) return this.clearHover();
    const t = p.time as number;
    const c = findAt(this.aggs.bars.out, t);
    if (!c) return this.clearHover();
    const st = useTerminal.getState();
    const tf = this.tf;
    const o: OddsAgg | null = findAt(this.aggs.odds.out, t);
    const f: FairAgg | null = findAt(this.aggs.fair.out, t);
    // evaluate "time left" at the candle's close (or now, for the in-progress candle)
    const refMs = Math.min((t + tf) * 1000, serverNow());
    const w = windowAt(windowList(st), refMs - 1);
    const ptb = w?.priceToBeat ?? null;
    const tLeft = w ? Math.max(0, (w.endTs - refMs) / 1000) : (f?.tLeftSec ?? null);
    const upMid = o?.upMid ?? null;
    const edge = f && upMid != null ? f.fair - upMid : (f?.edge ?? null);

    if (t !== this.hoverTime) {
      this.hoverTime = t;
      st.setHover({
        time: t * 1000,
        o: c.open,
        h: c.high,
        l: c.low,
        c: c.close,
        upMid,
        upBid: o?.upBid ?? null,
        upAsk: o?.upAsk ?? null,
        fair: f?.fair ?? null,
        edge,
        ptb,
        tLeftSec: tLeft,
      });
      this.hoverEdge = edge;
      if (this.edge) {
        this.updateEdgeLabel();
        this.edgeLabel.update();
      }
    }

    const d = ptb != null ? c.close - ptb : null;
    const rect = this.container.getBoundingClientRect();
    const ev = p.sourceEvent;
    const x = ev ? ev.clientX - rect.left : p.point.x + this.chart.priceScale("left").width();
    const y = ev ? ev.clientY - rect.top : p.point.y;
    this.tooltip.show(
      {
        timeLabel: `${tf >= 60 ? hmAp(t * 1000) : `${hms(t * 1000)} ${hmAp(t * 1000).slice(-2)}`} ET`,
        btc: fmtBtc(c.close),
        dPtb: d == null ? null : fmtSignedUsd(d),
        dPtbColor: d != null && d >= 0 ? C.up : C.down,
        upBA:
          o && (o.upBid != null || o.upAsk != null)
            ? `${o.upBid != null ? fmtCents(o.upBid) : "—"} / ${o.upAsk != null ? fmtCents(o.upAsk) : "—"}`
            : null,
        fair: f ? fmtCents(f.fair) : null,
        edge: edge == null ? null : fmtSignedCents(edge),
        edgeColor: edge != null && edge >= 0 ? C.oddsText : C.negEdge,
        tLeft: tLeft == null ? null : mss(tLeft),
      },
      x,
      y,
      rect.width,
      rect.height,
    );
  };

  private clearHover() {
    this.tooltip.hide();
    if (this.hoverTime == null) return;
    this.hoverTime = null;
    this.hoverEdge = null;
    useTerminal.getState().setHover(null);
    if (this.edge) {
      this.updateEdgeLabel();
      this.edgeLabel.update();
    }
  }
}

// ---- point mappers -----------------------------------------------------------------------------------

function candlePoint(c: CandleAgg): CandlestickData<Time> {
  return { time: T(c.time), open: c.open, high: c.high, low: c.low, close: c.close };
}

/** Line point that hides the segment crossing into a different market window (segment color = start point). */
function breakLine(o: Bucketed & { windowId: string }, next: { windowId: string } | undefined, v: number | null): LinePoint {
  if (v == null) return { time: T(o.time) };
  if (next && next.windowId !== o.windowId) return { time: T(o.time), value: v, color: TRANSPARENT };
  return { time: T(o.time), value: v };
}

function edgePoint(f: FairAgg): HistogramData<Time> | WhitespaceData<Time> {
  if (f.edge == null) return { time: T(f.time) };
  return { time: T(f.time), value: f.edge, color: f.edge >= 0 ? C.odds : C.negEdge };
}

function exPoint(t: TickAgg, ex: ExchangeId): LinePoint {
  const v = t[ex];
  return v == null ? { time: T(t.time) } : { time: T(t.time), value: v };
}
