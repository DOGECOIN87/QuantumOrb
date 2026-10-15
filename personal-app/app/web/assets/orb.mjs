export class Orb {
  constructor(canvas, button) {
    this.canvas = canvas;
    this.button = button;
    this.context = canvas.getContext("2d");
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.moving = !this.reduced;
    this.visible = true;
    this.rotation = 0;
    this.pointer = 0;
    this.frame = 0;
    this.points = Array.from({ length: 1100 }, (_, i) => {
      const y = 1 - (2 * (i + 0.5)) / 1100,
        radius = Math.sqrt(1 - y * y),
        theta = i * 2.3999632297;
      return {
        x: Math.cos(theta) * radius,
        y,
        z: Math.sin(theta) * radius,
        gold: i % 13 < 6,
        size: i % 17 === 0 ? 1.5 : 0.65,
      };
    });
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(canvas);
    this.intersection = new IntersectionObserver(([entry]) => {
      this.visible = entry.isIntersecting;
      this.schedule();
    });
    this.intersection.observe(canvas);
    button.addEventListener("click", () => {
      this.moving = !this.moving;
      this.updateButton();
      this.schedule();
    });
    canvas.addEventListener("pointermove", (event) => {
      const rect = canvas.getBoundingClientRect();
      this.pointer = (event.clientX - rect.left) / rect.width - 0.5;
    });
    canvas.addEventListener("pointerleave", () => {
      this.pointer = 0;
    });
    document.addEventListener("visibilitychange", () => this.schedule());
    this.updateButton();
    this.resize();
    this.schedule();
  }
  updateButton() {
    this.button.textContent = `MOTION ${this.moving ? "ON" : "OFF"}`;
    this.button.setAttribute("aria-pressed", String(this.moving));
  }
  resize() {
    const box = this.canvas.getBoundingClientRect();
    this.width = box.width;
    this.height = box.height;
    this.dpr = Math.min(devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(box.width * this.dpr);
    this.canvas.height = Math.round(box.height * this.dpr);
    this.draw();
  }
  schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      if (!this.visible || document.hidden) return;
      this.draw();
      if (this.moving) this.schedule();
    });
  }
  draw() {
    const ctx = this.context;
    if (!ctx || !this.width) return;
    if (this.moving) this.rotation += 0.0015;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    const cx = this.width * 0.51,
      cy = this.height * 0.49,
      r = Math.min(this.width * 0.46, this.height * 0.425),
      angle = this.rotation + this.pointer * 0.14;
    const glow = ctx.createRadialGradient(cx, cy, r * 0.1, cx, cy, r * 1.2);
    glow.addColorStop(0, "#0c233425");
    glow.addColorStop(0.7, "#14355715");
    glow.addColorStop(1, "#0d172400");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, this.width, this.height);
    ctx.strokeStyle = "#31547b30";
    ctx.lineWidth = 0.5;
    for (let i = 0; i < 3; i++) {
      ctx.beginPath();
      ctx.ellipse(
        cx,
        cy,
        r,
        r * (0.25 + i * 0.21),
        -0.5 + i * 0.4,
        0,
        Math.PI * 2,
      );
      ctx.stroke();
    }
    const projected = this.points
      .map((p, i) => {
        const x = p.x * Math.cos(angle) + p.z * Math.sin(angle),
          z = p.z * Math.cos(angle) - p.x * Math.sin(angle);
        const tilt = p.y * 0.96 - z * 0.28,
          depth = z * 0.96 + p.y * 0.28,
          scale = 1 + depth * 0.11;
        return {
          x: cx + x * r * scale,
          y: cy + tilt * r * scale,
          depth,
          gold: p.gold && x < 0.17,
          size: p.size,
          index: i,
        };
      })
      .sort((a, b) => a.depth - b.depth);
    for (const p of projected) {
      const alpha = 0.18 + (p.depth + 1) * 0.3;
      if (p.gold && p.index % 2 === 0) {
        ctx.strokeStyle = `rgba(211,181,60,${alpha * 0.6})`;
        ctx.lineWidth = 0.55;
        ctx.beginPath();
        ctx.moveTo(cx + (p.x - cx) * 0.35, cy + (p.y - cy) * 0.35);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
      } else if (p.index % 7 === 0) {
        const other = projected[(p.index * 19) % projected.length];
        if (Math.hypot(other.x - p.x, other.y - p.y) < r * 0.24) {
          ctx.strokeStyle = `rgba(82,139,190,${alpha * 0.2})`;
          ctx.lineWidth = 0.5;
          ctx.beginPath();
          ctx.moveTo(p.x, p.y);
          ctx.lineTo(other.x, other.y);
          ctx.stroke();
        }
      }
      ctx.fillStyle = p.gold
        ? `rgba(232,200,72,${alpha})`
        : `rgba(115,174,216,${alpha})`;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size * (0.65 + (p.depth + 1) * 0.22), 0, Math.PI * 2);
      ctx.fill();
      if (p.gold && p.index % 9 === 0) {
        ctx.strokeStyle = `rgba(225,194,67,${alpha * 0.65})`;
        ctx.lineWidth = 0.6;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3.1, 0, Math.PI * 2);
        ctx.stroke();
      }
    }
    ctx.strokeStyle = "#679ec850";
    ctx.lineWidth = 0.6;
    ctx.beginPath();
    ctx.moveTo(cx, cy + r + 8);
    ctx.lineTo(cx, this.height - 15);
    ctx.stroke();
    ctx.fillStyle = "#8cb7da";
    ctx.beginPath();
    ctx.arc(cx, this.height - 15, 1.3, 0, Math.PI * 2);
    ctx.fill();
    if (!this.moving) this.button.textContent = "MOTION OFF";
  }
}
