/* vendored from github.com/alhazjm/cards @ d9f01e5 (v1.1.0) — do not edit here */
/*!
 * The Muted Card System (@alhazjm/cards) v1.1.0 — global build
 * MIT licensed. https://github.com/alhazjm/cards
 *
 * GENERATED FILE — do not edit. Rebuild with `npm run build`.
 * Source of truth is src/*.js.
 *
 * Exposes window.MutedCardSystem. For ESM, import from src/index.js instead.
 */
(function (global) {
  'use strict';

  // --- src/color.js ---
/**
 * Just enough colour science to check the system's own claims.
 *
 * The reference sheet asserts "AA 4.5:1 on every label, >=7:1 on the amount,
 * for all 47 cards, at any motif strength". That is a testable statement, and
 * this module is what makes it a test instead of a promise: OKLCH in, sRGB
 * out, alpha-composite the text over the base, compute WCAG 2.x contrast.
 *
 * Zero dependencies, because the whole library has zero dependencies.
 *
 * Conversion is Björn Ottosson's OKLab matrices. Values outside sRGB are
 * clipped per channel and flagged — the sheet's own note about violet and blue
 * running out of gamut near the top of the Paper band is exactly this, and a
 * clipped colour is what a screen will actually show, so clipping before
 * measuring contrast is the honest thing to do.
 */

const OKLCH_RE = /^\s*oklch\(\s*([^\s/]+)\s+([^\s/]+)\s+([^\s/]+)\s*(?:\/\s*([^\s)]+)\s*)?\)\s*$/i;
const RGB_RE = /^\s*rgba?\(([^)]+)\)\s*$/i;
const HEX_RE = /^#([0-9a-f]{3,8})$/i;

/** Parse `50%` / `.5` / `1` into a 0-1 number. */
function num(v, scale = 1) {
  const s = String(v).trim();
  if (s.endsWith('%')) return (parseFloat(s) / 100) * scale;
  return parseFloat(s);
}

/**
 * @param {string} str An `oklch(L C H)` or `oklch(L C H / A)` string.
 * @returns {{l: number, c: number, h: number, alpha: number}|null}
 */
function parseOklch(str) {
  const m = OKLCH_RE.exec(String(str));
  if (!m) return null;
  return {
    l: num(m[1]),
    c: num(m[2]),
    h: parseFloat(m[3]) || 0,
    alpha: m[4] === undefined ? 1 : num(m[4]),
  };
}

function gamma(x) {
  return x <= 0.0031308 ? 12.92 * x : 1.055 * Math.pow(x, 1 / 2.4) - 0.055;
}

function degamma(x) {
  return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
}

/**
 * OKLCH -> 8-bit sRGB.
 *
 * @param {{l: number, c: number, h: number}} oklch
 * @returns {{r: number, g: number, b: number, clipped: boolean}} Channels are
 *   0-255 floats. `clipped` is true when the colour was outside sRGB and had
 *   to be pulled back in — i.e. the screen cannot show the requested colour.
 */
function oklchToSrgb({ l, c, h }) {
  const hr = (h * Math.PI) / 180;
  const a = c * Math.cos(hr);
  const b = c * Math.sin(hr);

  const l_ = l + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = l - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = l - 0.0894841775 * a - 1.291485548 * b;

  const L = l_ * l_ * l_;
  const M = m_ * m_ * m_;
  const S = s_ * s_ * s_;

  const lin = [
    4.0767416621 * L - 3.3077115913 * M + 0.2309699292 * S,
    -1.2684380046 * L + 2.6097574011 * M - 0.3413193965 * S,
    -0.0041960863 * L - 0.7034186147 * M + 1.707614701 * S,
  ];

  let clipped = false;
  const out = lin.map((v) => {
    const enc = gamma(v);
    if (enc < -0.0001 || enc > 1.0001) clipped = true;
    return Math.min(255, Math.max(0, enc * 255));
  });

  return { r: out[0], g: out[1], b: out[2], clipped };
}

/**
 * Parse any colour this library emits into 8-bit RGBA.
 *
 * Handles `oklch()`, `rgb()`/`rgba()` in both the comma and space spellings,
 * and 3/4/6/8-digit hex. Anything else returns null rather than guessing.
 *
 * @param {string} str
 * @returns {{r: number, g: number, b: number, a: number, clipped?: boolean}|null}
 */
function parseColor(str) {
  const s = String(str).trim();

  const ok = parseOklch(s);
  if (ok) {
    const rgb = oklchToSrgb(ok);
    return { r: rgb.r, g: rgb.g, b: rgb.b, a: ok.alpha, clipped: rgb.clipped };
  }

  const rgbm = RGB_RE.exec(s);
  if (rgbm) {
    const parts = rgbm[1].includes(',') ? rgbm[1].split(',') : rgbm[1].split('/').join(' ').split(/\s+/);
    const vals = parts.map((p) => p.trim()).filter(Boolean);
    return {
      r: num(vals[0], 255),
      g: num(vals[1], 255),
      b: num(vals[2], 255),
      a: vals[3] === undefined ? 1 : num(vals[3]),
    };
  }

  const hexm = HEX_RE.exec(s);
  if (hexm) {
    let hx = hexm[1];
    if (hx.length === 3 || hx.length === 4) hx = hx.split('').map((ch) => ch + ch).join('');
    const int = (i) => parseInt(hx.slice(i, i + 2), 16);
    return { r: int(0), g: int(2), b: int(4), a: hx.length === 8 ? int(6) / 255 : 1 };
  }

  if (s.toLowerCase() === 'transparent') return { r: 0, g: 0, b: 0, a: 0 };
  if (s.toLowerCase() === 'white') return { r: 255, g: 255, b: 255, a: 1 };
  if (s.toLowerCase() === 'black') return { r: 0, g: 0, b: 0, a: 1 };

  return null;
}

/**
 * Alpha-composite `fg` over an opaque `bg`, the way a browser does it — in
 * encoded sRGB, not in linear light.
 *
 * @param {{r: number, g: number, b: number, a: number}} fg
 * @param {{r: number, g: number, b: number}} bg
 * @returns {{r: number, g: number, b: number}}
 */
function composite(fg, bg) {
  const a = fg.a === undefined ? 1 : fg.a;
  return {
    r: fg.r * a + bg.r * (1 - a),
    g: fg.g * a + bg.g * (1 - a),
    b: fg.b * a + bg.b * (1 - a),
  };
}

/**
 * WCAG 2.x relative luminance.
 * @param {{r: number, g: number, b: number}} rgb 0-255 channels.
 * @returns {number} 0-1.
 */
function relativeLuminance({ r, g, b }) {
  const R = degamma(r / 255);
  const G = degamma(g / 255);
  const B = degamma(b / 255);
  return 0.2126 * R + 0.7152 * G + 0.0722 * B;
}

/**
 * WCAG 2.x contrast ratio between two colours. Any alpha on either colour is
 * resolved against `over` (default white) first, so passing a translucent text
 * token and an opaque base does the right thing.
 *
 * @param {string|object} fg
 * @param {string|object} bg
 * @returns {number} 1-21.
 */
function contrastRatio(fg, bg) {
  const f = typeof fg === 'string' ? parseColor(fg) : fg;
  const b = typeof bg === 'string' ? parseColor(bg) : bg;
  if (!f || !b) return NaN;

  const bgOpaque = b.a === undefined || b.a === 1 ? b : composite(b, { r: 255, g: 255, b: 255 });
  const fgOpaque = composite(f, bgOpaque);

  const l1 = relativeLuminance(fgOpaque);
  const l2 = relativeLuminance(bgOpaque);
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

  // --- src/motifs.js ---
/**
 * The motif library — 23 families, all pure CSS.
 *
 * Rule 03 of the recipe: a motif carries *lightness only*. Every family is
 * painted white-on-Ink or black-on-Paper and nothing else. No motif carries
 * colour, so no motif can threaten type. Nothing here is traced from, or
 * derived from, any issuer's card artwork — these are generic geometric
 * families named for what they draw.
 *
 * Authored alphas span 3-11%. Most marks sit inside 4-9%; the two ends are
 * `plain` at 3% (barely a top-edge gradient) and `brush` at 11% (one soft
 * sweep with nothing else on the tile competing with it). Those exact numbers
 * are pinned in `test/motifs.test.js`, because a docstring drifts and a test
 * does not.
 *
 * Each family is a `background-image` value. Some also need a fixed
 * `background-size` (see `motifSize`); the rest tile at `auto`.
 */

/** Canonical family ids, in library order. `plain` is the fallback. */
const MOTIFS = [
  'facet',
  'crossplane',
  'ripple',
  'guilloche',
  'blob',
  'chevron',
  'contour',
  'rib',
  'brush',
  'bloom',
  'spiral',
  'emboss',
  'wordmark',
  'burst',
  'lace',
  'dotgrid',
  'wave',
  'swirl',
  'sheen',
  'converge',
  'dotline',
  'panel',
  'plain',
];

/** The two lightness bands. See `tokens.js` for the text tokens each implies. */
const BANDS = ['ink', 'paper'];

/**
 * Build the `background-image` for one motif family.
 *
 * @param {string} kind    A family id from `MOTIFS`. Anything unknown falls
 *                         back to `plain`, on purpose — a card with no usable
 *                         motif still reads correctly.
 * @param {string} band    `'ink'` (white marks) or `'paper'` (near-black marks).
 * @param {number} [strength=1.5]  Alpha multiplier. Clamped to [0, 1.8]. At
 *                         the 1.5 default the authored 3-11% alphas render at
 *                         4.5-16.5%; at the 1.8 ceiling, at most 19.8%.
 * @returns {string} A CSS `background-image` value.
 */
function motifImage(kind, band, strength = 1.5) {
  const k = Math.min(1.8, Math.max(0, Number(strength) || 0));

  // Every gradient mark in the library goes through this helper. `chevron` is
  // the one exception — it is a stroked SVG, so its alpha rides on
  // `stroke-opacity` instead — but it derives that alpha from the same clamped
  // `k`, and paints the same two band colours. So the guarantee is unchanged:
  // no code path in this file can emit a saturated or fully-opaque motif mark.
  const c = (a) =>
    band === 'ink'
      ? `rgba(255,255,255,${+(a * k).toFixed(4)})`
      : `rgba(15,20,32,${+(a * k).toFixed(4)})`;

  switch (kind) {
    case 'facet':
      return `conic-gradient(from 196deg at 24% 30%,${c(0.075)} 0 40deg,transparent 40deg 92deg,${c(0.042)} 92deg 148deg,transparent 148deg),conic-gradient(from 14deg at 44% 68%,${c(0.055)} 0 28deg,transparent 28deg 74deg,${c(0.075)} 74deg 118deg,transparent 118deg),linear-gradient(112deg,${c(0.04)} 0 16%,transparent 16% 33%,${c(0.058)} 33% 40%,transparent 40%)`;
    case 'crossplane':
      return `linear-gradient(116deg,${c(0.095)} 0 27%,transparent 27.3%),linear-gradient(-64deg,${c(0.05)} 0 33%,transparent 33.3%),linear-gradient(148deg,${c(0.062)} 0 54%,transparent 54.3%)`;
    case 'ripple':
      return `radial-gradient(circle at 76% 16%,transparent 0 44%,${c(0.1)} 44% 45.6%,transparent 45.6%),repeating-radial-gradient(circle at 76% 16%,${c(0.05)} 0 1px,transparent 1px 14px),radial-gradient(120% 110% at 76% 12%,${c(0.06)},transparent 60%)`;
    case 'guilloche':
      return `repeating-conic-gradient(from 0deg at 60% 52%,${c(0.075)} 0 .35deg,transparent .35deg 2.4deg),repeating-radial-gradient(circle at 60% 52%,${c(0.055)} 0 1px,transparent 1px 8px),radial-gradient(circle at 60% 52%,${c(0.1)} 0 11%,transparent 11.4%)`;
    case 'blob':
      return `radial-gradient(circle at 18% 84%,${c(0.085)} 0 12%,transparent 12.4%),radial-gradient(circle at 44% 90%,${c(0.07)} 0 10%,transparent 10.4%),radial-gradient(circle at 68% 82%,${c(0.09)} 0 13%,transparent 13.4%),radial-gradient(circle at 92% 90%,${c(0.07)} 0 10.5%,transparent 10.9%)`;
    case 'chevron': {
      // The one family that needs an SVG: a stroked polyline has no gradient
      // equivalent. Colour is percent-encoded because it sits in a data: URL.
      const st = band === 'ink' ? '%23ffffff' : '%230f1420';
      return `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='22' height='15'%3E%3Cpath d='M-2 12 4.5 3 11 12 17.5 3 24 12' fill='none' stroke='${st}' stroke-opacity='${+(0.1 * k).toFixed(4)}' stroke-width='3.4'/%3E%3C/svg%3E")`;
    }
    case 'contour':
      return `repeating-radial-gradient(ellipse 130% 62% at 26% 128%,${c(0.07)} 0 1px,transparent 1px 17px),repeating-radial-gradient(ellipse 90% 50% at 88% -20%,${c(0.055)} 0 1px,transparent 1px 14px)`;
    case 'rib':
      return `repeating-linear-gradient(90deg,${c(0.075)} 0 2px,transparent 2px 12px),linear-gradient(180deg,${c(0.045)},transparent 34%)`;
    case 'brush':
      return `radial-gradient(54% 20% at 40% 46%,${c(0.11)},transparent 74%),radial-gradient(46% 15% at 68% 64%,${c(0.085)},transparent 72%),radial-gradient(38% 13% at 56% 30%,${c(0.065)},transparent 72%)`;
    case 'bloom':
      return `radial-gradient(circle at 70% 30%,${c(0.08)} 0 7%,transparent 7.4%),radial-gradient(circle at 84% 54%,${c(0.07)} 0 10%,transparent 10.4%),radial-gradient(circle at 58% 70%,${c(0.062)} 0 8%,transparent 8.4%),radial-gradient(circle at 94% 22%,${c(0.05)} 0 5.5%,transparent 5.9%),radial-gradient(circle at 72% 90%,${c(0.055)} 0 7%,transparent 7.4%)`;
    case 'spiral':
      return `repeating-radial-gradient(circle at 70% 48%,${c(0.075)} 0 1.5px,transparent 1.5px 7px)`;
    case 'emboss':
      return `repeating-linear-gradient(0deg,${c(0.058)} 0 2px,transparent 2px 9px),linear-gradient(104deg,transparent 40%,${c(0.04)} 60%,transparent 78%)`;
    case 'wordmark':
      // "Oversized glyph mass" — the *silhouette* of a large letterform sitting
      // off the edge, not any particular letter and not any issuer's type.
      return `radial-gradient(56% 66% at 62% 52%,${c(0.095)},transparent 72%),linear-gradient(90deg,transparent 38%,${c(0.05)} 56%,transparent 74%)`;
    case 'burst':
      return `repeating-conic-gradient(from 250deg at 84% 4%,${c(0.062)} 0 1.4deg,transparent 1.4deg 9deg),radial-gradient(70% 80% at 84% 4%,${c(0.075)},transparent 66%)`;
    case 'lace':
      return `repeating-radial-gradient(circle at 20% 20%,${c(0.055)} 0 1px,transparent 1px 9px),repeating-radial-gradient(circle at 80% 72%,${c(0.055)} 0 1px,transparent 1px 11px)`;
    case 'dotgrid':
      return `radial-gradient(${c(0.1)} 1.4px,transparent 1.5px)`;
    case 'wave':
      return `repeating-radial-gradient(ellipse 140% 38% at 18% 50%,${c(0.075)} 0 1.5px,transparent 1.5px 12px)`;
    case 'swirl':
      return `radial-gradient(62% 90% at 20% 30%,${c(0.095)},transparent 64%),radial-gradient(70% 80% at 86% 70%,${c(0.075)},transparent 62%),radial-gradient(50% 60% at 60% 12%,${c(0.055)},transparent 60%)`;
    case 'sheen':
      return `linear-gradient(104deg,transparent 0 22%,${c(0.095)} 34%,transparent 46% 62%,${c(0.062)} 72%,transparent 84%)`;
    case 'converge':
      return `repeating-conic-gradient(from 202deg at 50% 128%,${c(0.07)} 0 .9deg,transparent .9deg 6deg)`;
    case 'dotline':
      return `repeating-radial-gradient(circle at 62% 50%,${c(0.075)} 0 1.2px,transparent 1.2px 6px)`;
    case 'panel':
      return `linear-gradient(180deg,transparent 0 56%,${c(0.09)} 56%)`;
    case 'plain':
    default:
      return `linear-gradient(180deg,${c(0.03)},transparent 60%)`;
  }
}

/**
 * The `background-size` a family needs. Only the two genuinely tiled families
 * pin a size; every other family is drawn once across the crest box.
 *
 * @param {string} kind A family id from `MOTIFS`.
 * @returns {string} A CSS `background-size` value.
 */
function motifSize(kind) {
  if (kind === 'dotgrid') return '11px 11px';
  if (kind === 'chevron') return '22px 15px';
  return 'auto';
}

/**
 * The library, as data — for building a motif index or a picker UI.
 * `band` / `swatchBase` are only the band and base each family is *shown* in
 * on the reference sheet; any family works in either band.
 */
const MOTIF_FAMILIES = [
  { id: 'facet',      label: 'Low-poly planes',      usedBy: 'HSBC · UOB EVOL',                   band: 'ink',   swatchBase: 'oklch(0.315 0.07 22)' },
  { id: 'crossplane', label: 'Hard diagonals',       usedBy: 'Live+ · Esso · Vantage',            band: 'ink',   swatchBase: 'oklch(0.30 0.078 22)' },
  { id: 'ripple',     label: 'Concentric arcs',      usedBy: 'Cash Back · Infinity · OCBC Rewards', band: 'ink', swatchBase: 'oklch(0.335 0.078 252)' },
  { id: 'guilloche',  label: 'Engraved fine line',   usedBy: 'Citi Rewards · Prestige',           band: 'ink',   swatchBase: 'oklch(0.315 0.052 196)' },
  { id: 'blob',       label: 'Disc cluster',         usedBy: 'Trust Cashback',                    band: 'paper', swatchBase: 'oklch(0.968 0.006 258)' },
  { id: 'chevron',    label: 'Zigzag',               usedBy: 'UOB One',                           band: 'paper', swatchBase: 'oklch(0.949 0.005 250)' },
  { id: 'contour',    label: 'Topographic rings',    usedBy: 'UOB Visa Signature',                band: 'ink',   swatchBase: 'oklch(0.322 0.03 128)' },
  { id: 'rib',        label: 'Vertical ribs',        usedBy: 'Maybank XL',                        band: 'paper', swatchBase: 'oklch(0.928 0.078 96)' },
  { id: 'brush',      label: 'Painted sweep',        usedBy: 'DBS Live Fresh',                    band: 'ink',   swatchBase: 'oklch(0.30 0.038 16)' },
  { id: 'bloom',      label: 'Soft scatter',         usedBy: "Woman's · Lady's Solitaire",        band: 'paper', swatchBase: 'oklch(0.902 0.055 298)' },
  { id: 'spiral',     label: 'Concentric spiral',    usedBy: "Takashimaya · UOB Lady's",          band: 'paper', swatchBase: 'oklch(0.90 0.058 302)' },
  { id: 'emboss',     label: 'Embossed relief',      usedBy: 'Altitude · Amex · Cashflo',         band: 'paper', swatchBase: 'oklch(0.945 0.005 250)' },
  { id: 'wordmark',   label: 'Oversized glyph mass', usedBy: 'yuu · OCBC 365',                    band: 'paper', swatchBase: 'oklch(0.955 0.016 26)' },
  { id: 'burst',      label: 'Light rays',           usedBy: 'OCBC 90°N',                         band: 'ink',   swatchBase: 'oklch(0.278 0.008 250)' },
  { id: 'lace',       label: 'Ornamental lace',      usedBy: 'UOB Visa Infinite',                 band: 'ink',   swatchBase: 'oklch(0.285 0.018 282)' },
  { id: 'dotgrid',    label: 'Dot lattice',          usedBy: 'UOB Lazada',                        band: 'ink',   swatchBase: 'oklch(0.30 0.05 286)' },
  { id: 'wave',       label: 'Dotted wave field',    usedBy: 'UOB Preferred',                     band: 'ink',   swatchBase: 'oklch(0.315 0.06 250)' },
  { id: 'swirl',      label: 'Soft colour swirl',    usedBy: 'UOB Singtel',                       band: 'ink',   swatchBase: 'oklch(0.32 0.055 16)' },
  { id: 'sheen',      label: 'Metallic sweep',       usedBy: 'PRVI Miles ×3 · Absolute',          band: 'ink',   swatchBase: 'oklch(0.292 0.016 62)' },
  { id: 'converge',   label: 'Converging lines',     usedBy: 'Citi SMRT',                         band: 'paper', swatchBase: 'oklch(0.956 0.006 250)' },
  { id: 'dotline',    label: 'Dotted outline',       usedBy: 'Citi M1',                           band: 'ink',   swatchBase: 'oklch(0.295 0.01 78)' },
  { id: 'panel',      label: 'Two-tone split',       usedBy: 'FRANK · SAFRA',                     band: 'paper', swatchBase: 'oklch(0.945 0.004 250)' },
  { id: 'plain',      label: 'Plain black',          usedBy: 'HSBC TravelOne · fallback',         band: 'ink',   swatchBase: 'oklch(0.272 0.006 250)' },
];

  // --- src/tokens.js ---
/**
 * Text tokens for the two lightness bands.
 *
 * Rule 01 bands every card into exactly one of these. Because the base colour
 * is confined to a narrow lightness range and the text column is washed flat
 * with that same base (rule 04), one fixed set of text colours per band clears
 * AA on every label — for every card, at any motif strength.
 *
 * That is what "contrast by construction" means here: contrast is a property
 * of the band, not something re-measured per card. `test/contrast.test.js`
 * proves it numerically for all 47 catalogue entries.
 *
 * The tile this library ships renders only `title` and `detail`. The other
 * roles exist because a real application puts its own content on these
 * surfaces, and the useful thing to hand it is a palette that is already
 * known-safe on both bands. Use them rather than inventing values on top of a
 * muted base — that is the one way to lose the guarantee.
 */

/**
 * Text roles, quietest last.
 *
 *   title    the card name. The one piece of text the shipped tile requires.
 *   detail   secondary identity — the last four digits on the shipped tile.
 *   strong   a headline you add. Held to 7:1 rather than 4.5:1, because large
 *            numerals set at low contrast are the most common failure in this
 *            kind of interface.
 *   muted    a supporting line.
 *   faint    the quietest text still held to AA. Nothing goes below it.
 */
const ROLES = ['title', 'detail', 'strong', 'muted', 'faint'];

/** Roles held to 4.5:1. `strong` is held higher — see CONTRAST_FLOORS. */
const LABEL_ROLES = ['title', 'detail', 'muted', 'faint'];

/** White-on-dark. Base lightness band: L 0.27-0.34. */
const INK = {
  title: 'rgba(255,255,255,.97)',
  detail: 'rgba(255,255,255,.8)',
  strong: '#fff',
  muted: 'rgba(255,255,255,.8)',
  faint: 'rgba(255,255,255,.76)',
  barTrack: 'rgba(255,255,255,.22)',
  barFill: 'rgba(255,255,255,.94)',
};

/** Near-black-on-light. Base lightness band: L 0.90-0.97. */
const PAPER = {
  title: 'oklch(0.22 0.02 258)',
  detail: 'oklch(0.40 0.012 258)',
  strong: 'oklch(0.18 0.02 258)',
  muted: 'oklch(0.41 0.014 258)',
  faint: 'oklch(0.41 0.014 258)',
  barTrack: 'oklch(0.87 0.01 258)',
  barFill: 'oklch(0.36 0.02 258)',
};

/**
 * The lightness window each band's base colour must sit inside, and the chroma
 * ceiling that applies to both (rule 02). `validateCard` enforces these, so a
 * card that would break the system fails the check rather than shipping as a
 * low-contrast tile.
 */
const BAND_RANGES = {
  ink: { minL: 0.27, maxL: 0.34 },
  paper: { minL: 0.9, maxL: 0.97 },
};

/** Rule 02: clamp chroma to <= 0.085 so hue survives but stops shouting. */
const MAX_CHROMA = 0.085;

/**
 * @param {string} band `'ink'` or `'paper'`.
 * @returns {typeof INK} The token set for that band. Unknown bands get PAPER,
 *   which is the safer failure: dark text on an unknown light surface.
 */
function tokensFor(band) {
  return band === 'ink' ? INK : PAPER;
}

  // --- src/catalog.js ---
/**
 * The card catalogue — 47 Singapore consumer credit cards.
 *
 * Each entry is the *whole* design of a tile: a band, a base colour, and one
 * motif family name. Nothing else. That is the point of the system — adding a
 * card is three values and an enum, not an art task.
 *
 * These describe how a tile *represents* a card. They are not reproductions
 * of anything: no issuer artwork, logo, photograph or traced vector is
 * included here or anywhere in this repository, and no motif is derived from a
 * specific piece of card art. Card names appear only to identify which product
 * a tile stands for. See TRADEMARKS.md.
 *
 * Colours are the hue of each card sampled and then muted per rules 01-02:
 * the hue angle is kept, lightness is snapped into one of two bands, and
 * chroma is capped at 0.085.
 */

/**
 * Cards issued as one piece of artwork across several networks. They
 * deliberately *share* a motif family: muting cannot separate what was never
 * visually separate, and giving them different motifs would be a lie about the
 * cards. The network goes in the label instead, and hue separates them only
 * where the physical cards genuinely differ (PRVI Miles does; yuu does not).
 *
 * Rule 05's "no two achromatic cards share a motif" is checked *per twin
 * group*, so these are the registered exceptions.
 *
 * Note: the reference sheet names three twin sets in prose (PRVI Miles, yuu,
 * 90°N). `dbs-altitude` is a fourth by exactly the same logic — one product
 * issued on Visa and on Amex — and is registered here so the rule-05 invariant
 * in `test/catalog.test.js` is honest rather than special-cased.
 */
const TWIN_GROUPS = {
  'uob-prvi-miles': ['uob-prvi-miles-amex', 'uob-prvi-miles-mastercard', 'uob-prvi-miles-visa'],
  'dbs-yuu': ['dbs-yuu-amex', 'dbs-yuu-visa'],
  'ocbc-90n': ['ocbc-90n-mastercard', 'ocbc-90n-visa'],
  'dbs-altitude': ['dbs-altitude', 'dbs-altitude-amex'],
};

/**
 * Chroma at or below this reads as achromatic — black, silver, charcoal, white
 * — where hue carries almost nothing and the motif becomes the only identity
 * cue. Roughly a third of the catalogue sits here.
 */
const ACHROMATIC_CHROMA = 0.02;

/** @type {ReadonlyArray<{id: string, name: string, issuer: string, group: string, band: 'ink'|'paper', base: string, motif: string, twin?: string}>} */
const CARDS = [
  // --- DBS ---
  { id: 'dbs-live-fresh',      name: 'DBS Live Fresh',      issuer: 'DBS',     group: 'DBS',  band: 'ink',   base: 'oklch(0.30 0.038 16)',    motif: 'brush' },
  { id: 'dbs-yuu-amex',        name: 'DBS yuu Amex',        issuer: 'DBS',     group: 'DBS',  band: 'ink',   base: 'oklch(0.282 0.016 252)',  motif: 'wordmark',   twin: 'dbs-yuu' },
  { id: 'dbs-yuu-visa',        name: 'DBS yuu Visa',        issuer: 'DBS',     group: 'DBS',  band: 'ink',   base: 'oklch(0.285 0.014 252)',  motif: 'wordmark',   twin: 'dbs-yuu' },
  { id: 'dbs-altitude',        name: 'DBS Altitude',        issuer: 'DBS',     group: 'DBS',  band: 'paper', base: 'oklch(0.945 0.005 250)',  motif: 'emboss',     twin: 'dbs-altitude' },
  { id: 'dbs-vantage',         name: 'DBS Vantage',         issuer: 'DBS',     group: 'DBS',  band: 'ink',   base: 'oklch(0.278 0.018 84)',   motif: 'crossplane' },
  { id: 'dbs-altitude-amex',   name: 'DBS Altitude Amex',   issuer: 'DBS',     group: 'DBS',  band: 'paper', base: 'oklch(0.95 0.004 250)',   motif: 'emboss',     twin: 'dbs-altitude' },
  { id: 'dbs-womans-world',    name: "DBS Woman's World",   issuer: 'DBS',     group: 'DBS',  band: 'paper', base: 'oklch(0.943 0.024 182)',  motif: 'bloom' },
  { id: 'dbs-womans',          name: "DBS Woman's",         issuer: 'DBS',     group: 'DBS',  band: 'paper', base: 'oklch(0.956 0.014 82)',   motif: 'bloom' },
  { id: 'dbs-takashimaya',     name: 'DBS Takashimaya',     issuer: 'DBS',     group: 'DBS',  band: 'ink',   base: 'oklch(0.29 0.03 12)',     motif: 'spiral' },
  { id: 'dbs-esso',            name: 'DBS Esso',            issuer: 'DBS',     group: 'DBS',  band: 'ink',   base: 'oklch(0.30 0.032 250)',   motif: 'crossplane' },
  { id: 'safra-dbs',           name: 'SAFRA DBS',           issuer: 'DBS',     group: 'DBS',  band: 'ink',   base: 'oklch(0.30 0.026 146)',   motif: 'panel' },

  // --- Citi ---
  { id: 'citi-cash-back-plus', name: 'Citi Cash Back+',     issuer: 'Citi',    group: 'Citi', band: 'ink',   base: 'oklch(0.335 0.078 252)',  motif: 'ripple' },
  { id: 'citi-cash-back',      name: 'Citi Cash Back',      issuer: 'Citi',    group: 'Citi', band: 'ink',   base: 'oklch(0.32 0.07 246)',    motif: 'ripple' },
  { id: 'citi-rewards',        name: 'Citi Rewards',        issuer: 'Citi',    group: 'Citi', band: 'ink',   base: 'oklch(0.315 0.052 196)',  motif: 'guilloche' },
  { id: 'citi-m1',             name: 'Citi M1',             issuer: 'Citi',    group: 'Citi', band: 'ink',   base: 'oklch(0.295 0.01 78)',    motif: 'dotline' },
  { id: 'citi-premiermiles',   name: 'Citi PremierMiles',   issuer: 'Citi',    group: 'Citi', band: 'paper', base: 'oklch(0.943 0.005 240)',  motif: 'guilloche' },
  { id: 'citi-smrt',           name: 'Citi SMRT',           issuer: 'Citi',    group: 'Citi', band: 'paper', base: 'oklch(0.956 0.006 250)',  motif: 'converge' },
  { id: 'citi-prestige',       name: 'Citi Prestige',       issuer: 'Citi',    group: 'Citi', band: 'ink',   base: 'oklch(0.28 0.02 250)',    motif: 'guilloche' },

  // --- UOB ---
  { id: 'uob-one',                 name: 'UOB One',                 issuer: 'UOB', group: 'UOB', band: 'paper', base: 'oklch(0.949 0.005 250)', motif: 'chevron' },
  { id: 'uob-preferred',           name: 'UOB Preferred',           issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.315 0.06 250)',  motif: 'wave' },
  { id: 'uob-evol',                name: 'UOB EVOL',                issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.30 0.05 252)',   motif: 'facet' },
  { id: 'uob-ladys',               name: "UOB Lady's",              issuer: 'UOB', group: 'UOB', band: 'paper', base: 'oklch(0.90 0.058 302)',  motif: 'spiral' },
  { id: 'uob-ladys-solitaire',     name: "UOB Lady's Solitaire",    issuer: 'UOB', group: 'UOB', band: 'paper', base: 'oklch(0.902 0.055 298)', motif: 'bloom' },
  { id: 'uob-prvi-miles-amex',     name: 'UOB PRVI Miles Amex',     issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.285 0.012 250)', motif: 'sheen', twin: 'uob-prvi-miles' },
  { id: 'uob-prvi-miles-mastercard', name: 'UOB PRVI Miles Mastercard', issuer: 'UOB', group: 'UOB', band: 'ink', base: 'oklch(0.292 0.016 62)', motif: 'sheen', twin: 'uob-prvi-miles' },
  { id: 'uob-prvi-miles-visa',     name: 'UOB PRVI Miles Visa',     issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.285 0.012 212)', motif: 'sheen', twin: 'uob-prvi-miles' },
  { id: 'uob-absolute',            name: 'UOB Absolute',            issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.34 0.075 56)',   motif: 'sheen' },
  { id: 'uob-krisflyer',           name: 'UOB KrisFlyer',           issuer: 'UOB', group: 'UOB', band: 'paper', base: 'oklch(0.93 0.062 86)',   motif: 'sheen' },
  { id: 'uob-visa-infinite',       name: 'UOB Visa Infinite',       issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.285 0.018 282)', motif: 'lace' },
  { id: 'uob-lazada',              name: 'UOB Lazada',              issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.30 0.05 286)',   motif: 'dotgrid' },
  { id: 'uob-singtel',             name: 'UOB Singtel',             issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.315 0.058 268)', motif: 'swirl' },
  { id: 'uob-visa-signature',      name: 'UOB Visa Signature',      issuer: 'UOB', group: 'UOB', band: 'ink',   base: 'oklch(0.322 0.03 128)',  motif: 'contour' },

  // --- HSBC ---
  { id: 'hsbc-revolution',     name: 'HSBC Revolution',     issuer: 'HSBC', group: 'HSBC', band: 'paper', base: 'oklch(0.955 0.009 28)',  motif: 'facet' },
  { id: 'hsbc-live-plus',      name: 'HSBC Live+',          issuer: 'HSBC', group: 'HSBC', band: 'ink',   base: 'oklch(0.30 0.078 22)',   motif: 'crossplane' },
  { id: 'hsbc-advance',        name: 'HSBC Advance',        issuer: 'HSBC', group: 'HSBC', band: 'ink',   base: 'oklch(0.315 0.07 22)',   motif: 'facet' },
  { id: 'hsbc-travelone',      name: 'HSBC TravelOne',      issuer: 'HSBC', group: 'HSBC', band: 'ink',   base: 'oklch(0.272 0.006 250)', motif: 'plain' },
  { id: 'hsbc-visa-infinite',  name: 'HSBC Visa Infinite',  issuer: 'HSBC', group: 'HSBC', band: 'ink',   base: 'oklch(0.275 0.014 250)', motif: 'facet' },

  // --- OCBC ---
  { id: 'ocbc-365',            name: 'OCBC 365',                  issuer: 'OCBC', group: 'OCBC', band: 'paper', base: 'oklch(0.955 0.016 26)',  motif: 'wordmark' },
  { id: 'ocbc-infinity',       name: 'OCBC Infinity',             issuer: 'OCBC', group: 'OCBC', band: 'ink',   base: 'oklch(0.28 0.012 200)',  motif: 'ripple' },
  { id: 'ocbc-90n-mastercard', name: 'OCBC 90°N Mastercard',      issuer: 'OCBC', group: 'OCBC', band: 'ink',   base: 'oklch(0.278 0.008 250)', motif: 'burst', twin: 'ocbc-90n' },
  { id: 'ocbc-90n-visa',       name: 'OCBC 90°N Visa',            issuer: 'OCBC', group: 'OCBC', band: 'ink',   base: 'oklch(0.28 0.008 250)',  motif: 'burst', twin: 'ocbc-90n' },
  { id: 'ocbc-rewards',        name: 'OCBC Rewards',              issuer: 'OCBC', group: 'OCBC', band: 'paper', base: 'oklch(0.935 0.03 292)',  motif: 'ripple' },
  { id: 'ocbc-nxt',            name: 'OCBC NXT',                  issuer: 'OCBC', group: 'OCBC', band: 'paper', base: 'oklch(0.956 0.02 236)',  motif: 'sheen' },
  { id: 'frank-by-ocbc',       name: 'FRANK by OCBC',             issuer: 'OCBC', group: 'OCBC', band: 'paper', base: 'oklch(0.945 0.004 250)', motif: 'panel' },
  { id: 'ocbc-ge-cashflo',     name: 'OCBC Great Eastern Cashflo', issuer: 'OCBC', group: 'OCBC', band: 'ink',  base: 'oklch(0.33 0.085 28)',   motif: 'emboss' },

  // --- Others ---
  { id: 'trust-cashback',      name: 'Trust Cashback',      issuer: 'Trust',   group: 'Others', band: 'paper', base: 'oklch(0.968 0.006 258)', motif: 'blob' },
  { id: 'maybank-xl',          name: 'Maybank XL',          issuer: 'Maybank', group: 'Others', band: 'paper', base: 'oklch(0.928 0.078 96)',  motif: 'rib' },
];

/** Display order for `group`, matching the reference sheet. */
const GROUP_ORDER = ['DBS', 'Citi', 'UOB', 'HSBC', 'OCBC', 'Others'];

const BY_ID = new Map(CARDS.map((c) => [c.id, c]));

/** Strip everything that varies between how two systems spell one card name. */
function fold(s) {
  return String(s)
    .toLowerCase()
    .replace(/[’']/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

const BY_NAME = new Map(CARDS.map((c) => [fold(c.name), c]));

/**
 * Look a card up by id, or by display name.
 *
 * Name matching is deliberately forgiving — an app's card list will not spell
 * things the way this catalogue does. It folds case, punctuation and
 * apostrophes, then falls back to the longest catalogue name contained in the
 * query (or containing it). Longest-match wins so that "UOB Lady's Solitaire"
 * never resolves to "UOB Lady's".
 *
 * @param {string} query An id or a display name.
 * @returns {object|null} The catalogue entry, or null if nothing matched.
 */
function resolveCard(query) {
  if (!query) return null;

  const raw = String(query).trim();
  if (BY_ID.has(raw)) return BY_ID.get(raw);

  const folded = fold(raw);
  if (!folded) return null;
  if (BY_NAME.has(folded)) return BY_NAME.get(folded);

  let best = null;
  for (const card of CARDS) {
    const name = fold(card.name);
    if (folded.includes(name) || name.includes(folded)) {
      if (!best || name.length > fold(best.name).length) best = card;
    }
  }
  return best;
}

/** @returns {Array<{label: string, cards: Array<object>}>} Cards grouped for display. */
function cardsByGroup() {
  return GROUP_ORDER.map((label) => ({
    label,
    cards: CARDS.filter((c) => c.group === label),
  }));
}

  // --- src/card.js ---
/**
 * Tile composition — turning `{ band, base, motif }` into ready-to-use CSS.
 *
 * A muted tile is four stacked layers, and the order is load-bearing:
 *
 *   1. root     the base colour, flat, plus the radius/shadow
 *   2. crest    the motif, pinned to the trailing 44%, masked so it fades in
 *               from the right and down across the header row (rule 04)
 *   3. wash     a left-to-right gradient of the tile's *own base colour*,
 *               opaque across the text column and gone by 74%. This is what
 *               lets the crest run at 150% without ever touching type.
 *   4. content  the text, on top of the wash
 *
 * Two ways to consume it:
 *   - `cardCssVars()` + `css/cards.css` — class-based, best for plain HTML and
 *     for a single-file page that cannot run a build step.
 *   - `cardStyles()` — inline style strings, best inside a framework.
 */


/**
 * Fixed geometry. These numbers are a coupled set, not independent knobs: the
 * crest width and the wash's 50%/74% stops are chosen together so that pattern
 * and type provably never overlap. Change one and you must re-check the other,
 * and re-run the contrast suite.
 *
 * The tile is drawn at the ID-1 ratio (85.60 x 53.98 mm = 1.586), the physical
 * proportion of a real payment card. With only a name and a digit group on it,
 * a content-sized box reads as a generic panel; the card ratio is what makes it
 * read as a card without any skeuomorphic decoration.
 */
/**
 * The crest mask, as a function of where the bottom fade begins.
 *
 * Two gradients, intersected: one fades the motif in from the right, the other
 * from the top so it never runs under the first line of text. The third stop
 * pair is the bottom fade — at the default `100%` the two stops coincide at
 * the very edge, so it is a no-op and the shipped tile is unchanged.
 *
 * @param {string} [fade='100%'] Where the motif starts fading out vertically.
 * @returns {string} A CSS `mask-image` value.
 */
function crestMaskFor(fade = '100%') {
  return (
    'linear-gradient(90deg,transparent 0,#000 48%),' +
    `linear-gradient(180deg,transparent 0,#000 36%,#000 ${fade},transparent)`
  );
}

const GEOMETRY = {
  radius: '16px',
  aspect: '1.586',
  padding: '14px 16px',
  crestWidth: '44%',
  /*
   * The crest box's own width/height ratio, and the reason v1.1 exists.
   *
   * Every motif is painted ONCE across the crest box using percentage
   * geometry, so the box's proportion IS the artwork's proportion — stretch
   * the box and you stretch the drawing. Letting the crest fill the tile is
   * only correct while the tile is at the card ratio; on a taller tile the
   * artwork deforms, most visibly for the families built from rings
   * (`wave`, `ripple`, `spiral`), which turn into ellipses.
   *
   * So the box is pinned instead of stretched. On a card-ratio tile this is
   * exactly the full height and changes nothing:
   *     0.44 x 1.586 = 0.698
   * which is the coupling `test/card.test.js` enforces. On a taller tile the
   * crest stops at its designed height and `crestFade` softens the cut; on a
   * shorter one it overflows and the card's own `overflow:hidden` crops it —
   * cropped, never deformed.
   */
  crestAspect: '0.698',
  crestFade: '100%',
  // How far text may run before it would leave the washed column. Kept here
  // and in css/cards.css; test/card.test.js asserts the two agree.
  textColumn: '74%',
  crestMask: crestMaskFor('100%'),
  washStops: '0 50%',
  washEnd: '74%',
  shadow:
    '0 1px 2px oklch(0.4 0.03 258 / .12),0 10px 24px -15px oklch(0.4 0.03 258 / .3)',
};

const COLOR_FN = /^\s*(oklch|oklab|lch|lab|rgba?|hsla?|hwb|color)\((.*)\)\s*$/i;

/**
 * Produce a fully-transparent version of a colour that keeps its hue.
 *
 * Why not just `transparent`? `transparent` is transparent *black*. A gradient
 * running from a colour to transparent black used to interpolate through grey
 * and leave a dirty fringe across the middle of the tile. Fading to the base's
 * own `... / 0` keeps the ramp clean everywhere, including in browsers that do
 * not premultiply gradient interpolation.
 *
 * @param {string} color Any CSS colour. Function forms and hex are handled
 *   exactly; anything else falls back to `transparent` with the caveat above.
 * @returns {string} The same colour at alpha 0.
 */
function toTransparent(color) {
  const str = String(color).trim();

  const fn = COLOR_FN.exec(str);
  if (fn) {
    const [, name, args] = fn;
    // Drop any existing alpha, then re-add it as zero. Handles both the
    // `oklch(L C H)` and the `oklch(L C H / .5)` spellings, and the legacy
    // comma form `rgba(r,g,b,a)`.
    const slash = args.split('/');
    if (slash.length > 1) return `${name}(${slash[0].trim()} / 0)`;
    if (/^(rgba|hsla)$/i.test(name) && args.split(',').length === 4) {
      return `${name}(${args.split(',').slice(0, 3).join(',')},0)`;
    }
    return `${name}(${args.trim()} / 0)`;
  }

  if (/^#([0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(str)) {
    const hex = str.slice(1);
    if (hex.length === 3 || hex.length === 4) return `#${hex.slice(0, 3)}0`;
    return `#${hex.slice(0, 6)}00`;
  }

  return 'transparent';
}

/**
 * Normalise whatever the caller passed as a card into the three values the
 * system actually needs. Missing or unknown values degrade rather than throw —
 * an unknown motif renders `plain`, an unknown band renders `paper`.
 *
 * @param {{band?: string, base?: string, motif?: string}} card
 */
function normalise(card) {
  const band = card && card.band === 'ink' ? 'ink' : 'paper';
  const base = (card && card.base) || (band === 'ink' ? 'oklch(0.272 0.006 250)' : 'oklch(0.955 0.005 250)');
  const motif = (card && card.motif) || 'plain';
  return { band, base, motif };
}

/**
 * @typedef {object} MutedCardOptions
 * @property {number} [motifStrength=1.5] Alpha multiplier for the motif,
 *   clamped to [0, 1.8]. 1.5 is the shipped default; 0 hides the motif.
 * @property {number} [scrim=1] Opacity of the wash layer, 0-1. Below ~0.6 the
 *   motif starts to creep under the text column — the control exists to *see*
 *   why the wash is there, not because turning it down is safe.
 */

/**
 * CSS custom properties for one card. Pair with `css/cards.css`.
 *
 * @param {{band: string, base: string, motif: string}} card
 * @param {MutedCardOptions} [options]
 * @returns {Record<string, string>} Custom-property name -> value.
 */
function cardCssVars(card, options = {}) {
  const { band, base, motif } = normalise(card);
  const { motifStrength = 1.5, scrim = 1 } = options;
  const t = tokensFor(band);

  return {
    '--mc-base': base,
    '--mc-base-fade': toTransparent(base),
    '--mc-motif': motifImage(motif, band, motifStrength),
    '--mc-motif-size': motifSize(motif),
    '--mc-scrim': String(Math.min(1, Math.max(0, scrim))),
    // Used by the shipped tile.
    '--mc-t-title': t.title,
    '--mc-t-detail': t.detail,
    // Available for content you add. Emitted unconditionally so that an app
    // extending the tile never has to reach back into the JS for a safe value.
    '--mc-t-strong': t.strong,
    '--mc-t-muted': t.muted,
    '--mc-t-faint': t.faint,
    '--mc-bar-track': t.barTrack,
    '--mc-bar-fill': t.barFill,
  };
}

/**
 * The same thing as a `style="..."` string, for the common case of setting it
 * on one element.
 *
 * @param {{band: string, base: string, motif: string}} card
 * @param {MutedCardOptions} [options]
 * @returns {string}
 */
function cardCssText(card, options) {
  const vars = cardCssVars(card, options);
  return Object.keys(vars)
    .map((k) => `${k}:${vars[k]}`)
    .join(';');
}

/**
 * Per-layer inline style strings, for renderers that do not want a stylesheet.
 * The four values map onto the four layers described at the top of this file.
 *
 * @param {{band: string, base: string, motif: string}} card
 * @param {MutedCardOptions} [options]
 * @returns {{band: string, base: string, tokens: object, root: string, crest: string, wash: string, content: string}}
 */
function cardStyles(card, options = {}) {
  const { band, base, motif } = normalise(card);
  const { motifStrength = 1.5, scrim = 1, crestFade = GEOMETRY.crestFade } = options;
  const t = tokensFor(band);
  const g = GEOMETRY;
  const fade = toTransparent(base);
  const alpha = Math.min(1, Math.max(0, scrim));
  const mask = crestMaskFor(crestFade);

  return {
    band,
    base,
    tokens: t,
    root: [
      'position:relative',
      'overflow:hidden',
      `border-radius:${g.radius}`,
      `aspect-ratio:${g.aspect}`,
      `box-shadow:${g.shadow}`,
      `background:${base}`,
    ].join(';'),
    crest: [
      'position:absolute',
      'top:0',
      'right:0',
      // Deliberately not `bottom:0` — see GEOMETRY.crestAspect. The box is
      // pinned to its designed proportion so the artwork cannot deform.
      `width:${g.crestWidth}`,
      'height:auto',
      `aspect-ratio:${g.crestAspect}`,
      'pointer-events:none',
      'background-repeat:repeat',
      `-webkit-mask-image:${mask}`,
      // WebKit's prefixed property predates `mask-composite` and spells the
      // intersect operator `source-in`. Both lines are required: Safari reads
      // the prefixed pair, everything current reads the unprefixed pair.
      '-webkit-mask-composite:source-in',
      `mask-image:${mask}`,
      'mask-composite:intersect',
      `background-image:${motifImage(motif, band, motifStrength)}`,
      `background-size:${motifSize(motif)}`,
    ].join(';'),
    wash: [
      'position:absolute',
      'inset:0',
      'pointer-events:none',
      `background:linear-gradient(90deg,${base} ${g.washStops},${fade} ${g.washEnd})`,
      `opacity:${alpha.toFixed(3)}`,
    ].join(';'),
    content: [
      'position:relative',
      'height:100%',
      'box-sizing:border-box',
      `padding:${g.padding}`,
      'display:flex',
      'flex-direction:column',
      'justify-content:space-between',
      'align-items:flex-start',
      'gap:6px',
    ].join(';'),
  };
}

  // --- src/validate.js ---
/**
 * The recipe, as an executable check.
 *
 * "Adding card 48" is supposed to take ten minutes with nothing to re-test.
 * That is only true if the rules are enforced somewhere. This is that
 * somewhere: `validateCard` checks rules 01-03 and the contrast floors for a
 * single card, `validateCatalog` checks rule 05, which is a property of the
 * set rather than of any one card.
 */




/**
 * WCAG floors the system commits to. `label` covers title/detail/muted/faint;
 * `strong` is held higher because it is the role a consumer reaches for when
 * setting a large number, which is where low contrast hurts most.
 */
const CONTRAST_FLOORS = { label: 4.5, strong: 7 };

/**
 * Check one card against rules 01, 02, 03 and the contrast floors.
 *
 * The base colour is measured *flat*, with no motif over it. That is sound
 * rather than a shortcut: rule 04 washes the text column in the base colour,
 * so the pixels behind every glyph are the base and nothing else, at any motif
 * strength. This is the whole reason the system does not need per-card
 * contrast checking at each strength setting.
 *
 * @param {{id?: string, band: string, base: string, motif: string}} card
 * @returns {{ok: boolean, errors: string[], warnings: string[], contrast: Record<string, number>}}
 */
function validateCard(card) {
  const errors = [];
  const warnings = [];
  const contrast = {};
  const label = (card && (card.id || card.name)) || '(unnamed)';

  const band = card && card.band;
  if (band !== 'ink' && band !== 'paper') {
    errors.push(`${label}: band must be "ink" or "paper", got ${JSON.stringify(band)}`);
    return { ok: false, errors, warnings, contrast };
  }

  if (!MOTIFS.includes(card.motif)) {
    errors.push(`${label}: motif "${card.motif}" is not in the library (rule 03)`);
  }

  const oklch = parseOklch(card.base);
  if (!oklch) {
    errors.push(`${label}: base must be an oklch() colour, got ${JSON.stringify(card.base)}`);
    return { ok: false, errors, warnings, contrast };
  }

  const range = BAND_RANGES[band];
  if (oklch.l < range.minL - 1e-9 || oklch.l > range.maxL + 1e-9) {
    errors.push(
      `${label}: rule 01 — L ${oklch.l} is outside the ${band} band (${range.minL}-${range.maxL})`
    );
  }

  if (oklch.c > MAX_CHROMA + 1e-9) {
    errors.push(`${label}: rule 02 — chroma ${oklch.c} exceeds the ${MAX_CHROMA} ceiling`);
  }

  // Not an error. The sheet documents this as a known ceiling: violet and blue
  // simply run out of sRGB near the top of the Paper band, and the fix is to
  // sit the card at the band floor, which is a design call, not a bug.
  if (oklchToSrgb(oklch).clipped) {
    warnings.push(`${label}: base is outside sRGB and will be clipped on screen`);
  }

  // Every role is measured, including the ones the shipped tile does not
  // render — an app that uses `strong` for its own headline is relying on this
  // check just as much as the tile is.
  const t = tokensFor(band);
  for (const role of LABEL_ROLES) {
    const ratio = contrastRatio(t[role], card.base);
    contrast[role] = Math.round(ratio * 100) / 100;
    if (ratio < CONTRAST_FLOORS.label) {
      errors.push(
        `${label}: ${role} contrast ${ratio.toFixed(2)}:1 is below the ${CONTRAST_FLOORS.label}:1 floor`
      );
    }
  }

  const strong = contrastRatio(t.strong, card.base);
  contrast.strong = Math.round(strong * 100) / 100;
  if (strong < CONTRAST_FLOORS.strong) {
    errors.push(
      `${label}: strong contrast ${strong.toFixed(2)}:1 is below the ${CONTRAST_FLOORS.strong}:1 floor`
    );
  }

  return { ok: errors.length === 0, errors, warnings, contrast };
}

/**
 * Check rule 05 across a whole set: within one band, no two achromatic cards
 * may share a motif family, because for those hue carries almost nothing and
 * the motif is the only cue left.
 *
 * Cards in the same `twin` group are collapsed to one entry first — network
 * twins are one artwork issued several times and are *supposed* to share.
 * Comparison is per band because a Paper card and an Ink card are already
 * separated by the largest cue the system has.
 *
 * @param {Array<object>} cards
 * @returns {{ok: boolean, errors: string[], warnings: string[]}}
 */
function validateCatalog(cards) {
  const errors = [];
  const warnings = [];

  const seenIds = new Set();
  for (const c of cards) {
    if (seenIds.has(c.id)) errors.push(`duplicate card id "${c.id}"`);
    seenIds.add(c.id);
    const r = validateCard(c);
    errors.push(...r.errors);
    warnings.push(...r.warnings);
  }

  /** @type {Map<string, string[]>} `${band}|${motif}` -> distinct owners */
  const achromatic = new Map();
  for (const c of cards) {
    const oklch = parseOklch(c.base);
    if (!oklch || oklch.c > ACHROMATIC_CHROMA) continue;
    const key = `${c.band}|${c.motif}`;
    const owner = c.twin || c.id;
    const owners = achromatic.get(key) || [];
    if (!owners.includes(owner)) owners.push(owner);
    achromatic.set(key, owners);
  }

  for (const [key, owners] of achromatic) {
    if (owners.length > 1) {
      const [band, motif] = key.split('|');
      errors.push(
        `rule 05 — achromatic ${band} cards share the "${motif}" motif: ${owners.join(', ')}`
      );
    }
  }

  return { ok: errors.length === 0, errors, warnings };
}

  const VERSION = '1.1.0';

  const MutedCardSystem = {
    ACHROMATIC_CHROMA,
    BANDS,
    BAND_RANGES,
    CARDS,
    CONTRAST_FLOORS,
    GEOMETRY,
    GROUP_ORDER,
    INK,
    LABEL_ROLES,
    MAX_CHROMA,
    MOTIFS,
    MOTIF_FAMILIES,
    PAPER,
    ROLES,
    TWIN_GROUPS,
    VERSION,
    cardCssText,
    cardCssVars,
    cardStyles,
    cardsByGroup,
    composite,
    contrastRatio,
    crestMaskFor,
    motifImage,
    motifSize,
    oklchToSrgb,
    parseColor,
    parseOklch,
    relativeLuminance,
    resolveCard,
    toTransparent,
    tokensFor,
    validateCard,
    validateCatalog,
  };

  if (typeof module === 'object' && module.exports) module.exports = MutedCardSystem;
  global.MutedCardSystem = MutedCardSystem;
})(typeof globalThis !== 'undefined' ? globalThis : this);
