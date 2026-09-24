/**
 * Lightweight canvas line chart — zero dependencies, DPI-aware.
 * Each chart owns N series internally (as rolling buffers).
 *
 * ES module split of the former app/static/charts.js — the core renderer was
 * extended with an optional shared time axis (x labels), per-series area
 * fill, dashed lines and alpha support used by the history/anomaly views.
 *
 * Backward compatible: charts without a ``times`` array render exactly as
 * before (no x-axis labels), ``fixedMax`` and anomaly markers are unchanged.
 */
function fmtAxisLabel(v) {
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  if (v >= 100) return `${Math.round(v)}`;
  return `${v.toFixed(1)}`;
}

export class LineChart {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {object} config - { series: [{color, label?, fixedMax?, area?, dash?, alpha?}], maxPoints? }
   */
  constructor(canvas, config) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.series = config.series;
    this.maxPoints = config.maxPoints || 90;
    this.values = this.series.map(() => []);
    this.markers = this.series.map(() => []);
    this.times = [];
    this.width = 0;
    this.height = 0;
    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
    this.resize();
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const parent = this.canvas.parentElement;
    const w = this.canvas.clientWidth || (parent ? parent.clientWidth : 0) || 300;
    const h = this.canvas.clientHeight || 150;
    this.canvas.width = Math.max(w, 1) * dpr;
    this.canvas.height = Math.max(h, 1) * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.width = w;
    this.height = h;
    this.redraw();
  }

  /** Grow the rolling buffer (used when seeding long history buckets). */
  setMaxPoints(n) {
    this.maxPoints = Math.max(2, n);
    this.redraw();
  }

  /** Replace all series buffers (e.g., seed from stored history).
   *  ``markerArrays`` provides one boolean per sample (true = marker/anomaly).
   *  ``times`` optionally provides an aligned epoch-ms timeline for x labels. */
  setData(arrays, markerArrays, times) {
    this.series.forEach((_, i) => {
      this.values[i] = (arrays[i] || []).slice(-this.maxPoints);
      const flags = (markerArrays && markerArrays[i]) || [];
      this.markers[i] = flags.slice(-this.maxPoints);
    });
    const n = Math.max(0, ...this.values.map((v) => v.length));
    this.times = times && times.length ? times.slice(-(n || 0)) : [];
    this.redraw();
  }

  /** Append one value per series; ``flags`` marks markers on this point.
   *  ``times`` optionally appends the corresponding epoch-ms timestamp. */
  push(values, flags, times) {
    this.series.forEach((_, i) => {
      this.values[i].push(values[i]);
      if (this.values[i].length > this.maxPoints) this.values[i].shift();
      this.markers[i].push(flags && flags[i] ? true : false);
      if (this.markers[i].length > this.maxPoints) this.markers[i].shift();
    });
    if (times && times.length) {
      this.times.push(times[0]);
      if (this.times.length > this.maxPoints) this.times.shift();
    }
    this.redraw();
  }

  _yMax() {
    // Use the largest fixedMax among series unless all auto.
    const fixed = this.series
      .map((s) => s.fixedMax)
      .filter((v) => v != null);
    if (fixed.length) return Math.max(...fixed);

    let max = 0;
    for (const vals of this.values) {
      for (const v of vals) if (v > max) max = v;
    }
    return Math.max(max * 1.15, 1);
  }

  /** Format a shared-timeline label based on the visible span. */
  _fmtTime(ms) {
    const d = new Date(ms);
    const p = (x) => String(x).padStart(2, "0");
    if (this.times.length > 1) {
      const span = this.times[this.times.length - 1] - this.times[0];
      if (span <= 5 * 60 * 1000) {
        return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
      }
      if (span <= 26 * 3600 * 1000) {
        return `${p(d.getHours())}:${p(d.getMinutes())}`;
      }
      return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    }
    return `${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  /** Vertical gridlines + time labels under the plot area. */
  _renderTimeAxis(ctx, W, H, labelH, top, bottom) {
    const n = this.times.length;
    if (!n || labelH <= 0) return;
    const ticks = 4;
    const stepX = W / (this.maxPoints - 1 || 1);
    for (let t = 0; t < ticks; t++) {
      const idx = Math.round((t * (n - 1)) / (ticks - 1 || 1));
      const x = W - (n - 1 - idx) * stepX;
      ctx.strokeStyle = "rgba(148,163,184,0.08)";
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      ctx.fillStyle = "rgba(148,163,184,0.6)";
      ctx.textAlign = "center";
      ctx.fillText(
        this._fmtTime(this.times[idx]),
        Math.min(Math.max(x, 20), W - 20),
        H - 3,
      );
    }
    ctx.textAlign = "left";
  }

  redraw() {
    const ctx = this.ctx;
    if (!ctx) return;
    const { width: W, height: H } = this;
    ctx.clearRect(0, 0, W, H);
    if (W < 10 || H < 10) return;

    const labelH = this.times.length ? 18 : 0;
    const top = 6;
    const bottom = H - 8 - labelH;
    const plotH = Math.max(bottom - top, 1);

    // Horizontal grid lines + y-max label.
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillStyle = "rgba(148,163,184,0.65)";
    ctx.strokeStyle = "rgba(148,163,184,0.15)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 3; i++) {
      const y = top + (plotH * i) / 3;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(W, y);
      ctx.stroke();
    }
    const yMax = this._yMax();
    ctx.textAlign = "left";
    ctx.fillText(fmtAxisLabel(yMax), 2, top + 7);
    ctx.fillStyle = "rgba(148,163,184,0.35)";
    ctx.fillText("0", 2, bottom + 3);

    this._renderTimeAxis(ctx, W, H, labelH, top, bottom);

    const stepX = W / (this.maxPoints - 1 || 1);
    const mapY = (v) => bottom - (Math.min(v, yMax) / yMax) * plotH;

    this.series.forEach((s, si) => {
      const vals = this.values[si];
      const flags = this.markers[si];
      const alpha = s.alpha == null ? 1 : s.alpha;

      if (!vals.length) {
        // Draw baseline so the chart still shows its grid/title.
        ctx.beginPath();
        ctx.moveTo(0, bottom);
        ctx.lineTo(W, bottom);
        ctx.strokeStyle = "rgba(148,163,184,0.25)";
        ctx.stroke();
        return;
      }
      if (vals.length === 1) {
        const x = W - stepX;
        const y = mapY(vals[0]);
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(x, y, 2.5, 0, Math.PI * 2);
        ctx.fill();
        if (flags && flags[flags.length - 1]) this._drawMarker(ctx, x, y);
        return;
      }

      const points = [];
      vals.forEach((v, i) => {
        points.push([W - (vals.length - 1 - i) * stepX, mapY(v)]);
      });

      // Optional translucent area fill under the line.
      if (s.area) {
        ctx.save();
        ctx.globalAlpha = alpha * 0.55;
        ctx.fillStyle = s.color;
        ctx.beginPath();
        points.forEach(([x, y], i) => {
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.lineTo(points[points.length - 1][0], bottom);
        ctx.lineTo(points[0][0], bottom);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
      }

      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      if (s.dash) ctx.setLineDash([5, 4]);
      ctx.beginPath();
      points.forEach(([x, y], i) => {
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();

      if (flags) {
        points.forEach(([x, y], i) => {
          if (flags[i]) this._drawMarker(ctx, x, y);
        });
      }
    });
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
  }

  /** Red dot (with white halo) highlighting an anomalous point. */
  _drawMarker(ctx, x, y) {
    ctx.fillStyle = "#E63946";
    ctx.strokeStyle = "rgba(255,255,255,0.85)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(x, y, 3.2, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
}