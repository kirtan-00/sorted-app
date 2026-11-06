/* @real-human/liquid-glass 1.2.0, MIT licence, vendored unchanged (the app is offline). https://www.npmjs.com/package/@real-human/liquid-glass */
function l({ height: r, width: t, radius: e, depth: s }) {
  const i = `<svg height="${r}" width="${t}" viewBox="0 0 ${t} ${r}" xmlns="http://www.w3.org/2000/svg">
        <style>
            .mix { mix-blend-mode: screen; }
        </style>
        <defs>
            <linearGradient 
              id="Y" 
              x1="0" 
              x2="0" 
              y1="${Math.ceil(e / r * 15)}%" 
              y2="${Math.floor(100 - e / r * 15)}%">
                <stop offset="0%" stop-color="#0F0" />
                <stop offset="100%" stop-color="#000" />
            </linearGradient>
            <linearGradient 
              id="X" 
              x1="${Math.ceil(e / t * 15)}%" 
              x2="${Math.floor(100 - e / t * 15)}%"
              y1="0" 
              y2="0">
                <stop offset="0%" stop-color="#F00" />
                <stop offset="100%" stop-color="#000" />
            </linearGradient>
        </defs>

        <rect x="0" y="0" height="${r}" width="${t}" fill="#808080" />
        <g filter="blur(2px)">
          <rect x="0" y="0" height="${r}" width="${t}" fill="#000080" />
          <rect x="0" y="0" height="${r}" width="${t}" fill="url(#Y)" class="mix" />
          <rect x="0" y="0" height="${r}" width="${t}" fill="url(#X)" class="mix" />
          <rect
              x="${s}"
              y="${s}"
              height="${r - 2 * s}"
              width="${t - 2 * s}"
              fill="#808080"
              rx="${e}"
              ry="${e}"
              filter="blur(${s}px)"
          />
        </g>
    </svg>`;
  return "data:image/svg+xml;utf8," + encodeURIComponent(i);
}
function c({
  height: r,
  width: t,
  radius: e,
  depth: s,
  strength: i = 100,
  chromaticAberration: a = 0
}) {
  const n = l({ height: r, width: t, radius: e, depth: s }), h = `<svg height="${r}" width="${t}" viewBox="0 0 ${t} ${r}" xmlns="http://www.w3.org/2000/svg">
        <defs>
            <filter id="displace" color-interpolation-filters="sRGB">
                <feImage x="0" y="0" height="${r}" width="${t}" href="${n}" result="displacementMap" />
                <feDisplacementMap
                    transform-origin="center"
                    in="SourceGraphic"
                    in2="displacementMap"
                    scale="${i + a * 2}"
                    xChannelSelector="R"
                    yChannelSelector="G"
                />
                <feColorMatrix type="matrix" values="1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0" result="displacedR" />
                <feDisplacementMap
                    in="SourceGraphic"
                    in2="displacementMap"
                    scale="${i + a}"
                    xChannelSelector="R"
                    yChannelSelector="G"
                />
                <feColorMatrix type="matrix" values="0 0 0 0 0 0 1 0 0 0 0 0 0 0 0 0 0 0 1 0" result="displacedG" />
                <feDisplacementMap
                        in="SourceGraphic"
                        in2="displacementMap"
                        scale="${i}"
                        xChannelSelector="R"
                        yChannelSelector="G"
                    />
                <feColorMatrix type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 1 0" result="displacedB" />
                <feBlend in="displacedR" in2="displacedG" mode="screen"/>
                <feBlend in2="displacedB" mode="screen"/>
            </filter>
        </defs>
    </svg>`;
  return "data:image/svg+xml;utf8," + encodeURIComponent(h) + "#displace";
}
class o extends HTMLElement {
  constructor() {
    super(), this.clicked = !1, this.attachShadow({ mode: "open" }), o._svgFilterSupport === void 0 && (o._svgFilterSupport = this.detectSVGFilterSupport());
  }
  detectSVGFilterSupport() {
    const t = document.createElement("div");
    if (t.style.backdropFilter = "blur(1px)", !t.style.backdropFilter) return !1;
    const e = navigator.userAgent.toLowerCase();
    if (/chrome|chromium|crios|edg/.test(e) && !/firefox|fxios/.test(e)) return !0;
    try {
      return t.style.backdropFilter = "url(#test)", t.style.backdropFilter.includes("url");
    } catch {
      return !1;
    }
  }
  get hasSVGFilterSupport() {
    return o._svgFilterSupport;
  }
  static get observedAttributes() {
    return [
      "width",
      "height",
      "radius",
      "depth",
      "blur",
      "strength",
      "chromatic-aberration",
      "debug",
      "background-color",
      "responsive",
      "base-width",
      "base-height",
      "auto-size",
      "min-width",
      "min-height"
    ];
  }
  connectedCallback() {
    this.render(), this.setupEventListeners(), this.setupResponsive(), this.autoSize && this.setupAutoSizeObserver();
  }
  setupAutoSizeObserver() {
    new MutationObserver(() => {
      setTimeout(() => this.updateStyles(), 0);
    }).observe(this, { childList: !0, subtree: !0, characterData: !0 }), window.ResizeObserver && new ResizeObserver(() => this.updateStyles()).observe(this.shadowRoot.querySelector(".glass-box"));
  }
  setupResponsive() {
    this.hasAttribute("responsive") && (this.updateResponsiveSize(), window.addEventListener("resize", () => this.updateResponsiveSize()));
  }
  updateResponsiveSize() {
    const t = parseInt(this.getAttribute("base-width") || this.getAttribute("width")) || 200, e = parseInt(this.getAttribute("base-height") || this.getAttribute("height")) || 200, s = window.innerWidth;
    let i = 1;
    s < 480 ? i = 0.6 : s < 768 ? i = 0.8 : s < 1024 && (i = 0.9);
    const a = Math.round(t * i), n = Math.round(e * i);
    (a !== this.width || n !== this.height) && (this.setAttribute("width", a), this.setAttribute("height", n));
  }
  attributeChangedCallback() {
    this.shadowRoot && this.render();
  }
  // Getters
  get width() {
    return parseInt(this.getAttribute("width")) || 200;
  }
  get height() {
    return parseInt(this.getAttribute("height")) || 200;
  }
  get radius() {
    return parseInt(this.getAttribute("radius")) || 50;
  }
  get baseDepth() {
    return parseInt(this.getAttribute("depth")) || 10;
  }
  get blur() {
    return parseInt(this.getAttribute("blur")) || 2;
  }
  get strength() {
    return parseInt(this.getAttribute("strength")) || 100;
  }
  get chromaticAberration() {
    return parseInt(this.getAttribute("chromatic-aberration")) || 0;
  }
  get debug() {
    return this.getAttribute("debug") === "true";
  }
  get backgroundColor() {
    return this.getAttribute("background-color") || "rgba(255, 255, 255, 0.4)";
  }
  get autoSize() {
    return this.hasAttribute("auto-size");
  }
  get minWidth() {
    return parseInt(this.getAttribute("min-width")) || 0;
  }
  get minHeight() {
    return parseInt(this.getAttribute("min-height")) || 0;
  }
  get depth() {
    return this.baseDepth / (this.clicked ? 0.7 : 1);
  }
  setupEventListeners() {
    const t = this.shadowRoot.querySelector(".glass-box"), e = (s) => {
      this.clicked = s, this.updateStyles();
    };
    t.addEventListener("mousedown", () => e(!0)), t.addEventListener("mouseup", () => e(!1)), t.addEventListener("mouseleave", () => e(!1)), document.addEventListener("mouseup", () => {
      this.clicked && e(!1);
    });
  }
  updateStyles() {
    const t = this.shadowRoot.querySelector(".glass-box");
    t && this.applyDynamicStyles(t);
  }
  applyDynamicStyles(t) {
    t.style.borderRadius = `${this.radius}px`;
    let e = this.width, s = this.height;
    if (this.autoSize) {
      t.style.backdropFilter = "none", t.style.background = "rgba(255, 255, 255, 0.4)", t.offsetWidth;
      const a = t.getBoundingClientRect();
      if (e = Math.ceil(a.width), s = Math.ceil(a.height), e === 0 || s === 0) {
        requestAnimationFrame(() => this.updateStyles());
        return;
      }
      e = Math.max(e, this.minWidth, 50), s = Math.max(s, this.minHeight, 30);
    } else
      t.style.height = `${this.height}px`, t.style.width = `${this.width}px`;
    const i = "1px 1px 1px 0px rgba(255,255,255, 0.60) inset, -1px -1px 1px 0px rgba(255,255,255, 0.60) inset, 0px 0px 16px 0px rgba(0,0,0, 0.04)";
    this.debug ? (t.style.background = `url("${l({
      height: s,
      width: e,
      radius: this.radius,
      depth: this.depth
    })}")`, t.style.boxShadow = "none", t.style.backdropFilter = "none") : this.hasSVGFilterSupport ? (t.style.backdropFilter = `blur(${this.blur / 2}px) url('${c({
      height: s,
      width: e,
      radius: this.radius,
      depth: this.depth,
      strength: this.strength,
      chromaticAberration: this.chromaticAberration
    })}') blur(${this.blur}px) brightness(1.1) saturate(1.5)`, t.style.background = this.backgroundColor, t.style.boxShadow = i) : (t.style.backdropFilter = `blur(${this.blur * 2}px)`, t.style.background = this.backgroundColor, t.style.boxShadow = i, t.style.border = "1px solid rgba(255, 255, 255, 0.3)");
  }
  render() {
    this.shadowRoot.innerHTML = `
            <style>
                :host { display: ${this.autoSize ? "inline-block" : "block"}; }
                .glass-box {
                    background: rgba(255, 255, 255, 0.4);
                    box-shadow: 1px 1px 1px 0px rgba(255,255,255, 0.60) inset, -1px -1px 1px 0px rgba(255,255,255, 0.60) inset, 0px 0px 16px 0px rgba(0,0,0, 0.04);
                    cursor: pointer;
                    transition: transform 0.1s ease;
                    position: relative;
                    ${this.autoSize ? `display: inline-block; width: fit-content; min-width: ${this.minWidth}px; min-height: ${this.minHeight}px;` : ""}
                }
                .glass-box:active { transform: scale(0.98); }
                .content {
                    ${this.autoSize ? "" : "width: 100%; height: 100%;"}
                    display: flex; align-items: center; justify-content: center;
                    color: white; text-align: center; font-family: sans-serif;
                    ${this.autoSize ? "padding: var(--glass-padding, 16px 24px);" : ""}
                }
            </style>
            <div class="glass-box">
                <div class="content"><slot></slot></div>
            </div>
        `;
    const t = this.shadowRoot.querySelector(".glass-box");
    this.autoSize ? requestAnimationFrame(() => requestAnimationFrame(() => this.applyDynamicStyles(t))) : this.applyDynamicStyles(t);
  }
}
customElements.get("glass-element") || customElements.define("glass-element", o);
export {
  o as GlassElement
};
