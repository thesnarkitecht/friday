//! Friday's mark: a coin with two faces. One is System 1 (the bolt), one is System 2 (the F).
//!
//! Drawn in Braille dots - each terminal cell is a 2x4 dot grid - as a real disc with thickness
//! turning on its vertical axis, so the rim shows as it passes edge-on.

use std::f64::consts::PI;
use std::io::Write;

const THICKNESS: f64 = 0.34;
const BOLT: [(f64, f64); 6] = [(0.20, -0.60), (-0.38, 0.10), (-0.06, 0.10), (-0.20, 0.60), (0.38, -0.12), (0.06, -0.12)];
pub const F_FACE: (f64, f64, f64) = (142.0, 162.0, 255.0);
pub const BOLT_FACE: (f64, f64, f64) = (245.0, 180.0, 84.0);
const RIM: (f64, f64, f64) = (96.0, 106.0, 140.0);
const DOTS: [[u32; 2]; 4] = [[0x01, 0x08], [0x02, 0x10], [0x04, 0x20], [0x40, 0x80]]; // braille bit for (row, col)
pub const ROWS: usize = 12;

fn in_bolt(x: f64, y: f64) -> bool {
    let mut inside = false;
    for i in 0..BOLT.len() {
        let ((x1, y1), (x2, y2)) = (BOLT[i], BOLT[(i + 1) % BOLT.len()]);
        if (y1 > y) != (y2 > y) && x < (x2 - x1) * (y - y1) / (y2 - y1) + x1 {
            inside = !inside;
        }
    }
    inside
}

fn in_f(u: f64, v: f64) -> bool {
    let stem = (-0.30..=-0.06).contains(&u) && (-0.5..=0.5).contains(&v);
    let top = (-0.30..=0.34).contains(&u) && (-0.5..=-0.30).contains(&v);
    let mid = (-0.30..=0.20).contains(&u) && (-0.10..=0.10).contains(&v);
    stem || top || mid
}

/// 0 = empty, 1 = a lit dot on the visible face, 2 = a lit dot on the rim.
fn sample(x: f64, y: f64, theta: f64, parity: bool) -> u8 {
    if y.abs() > 1.0 {
        return 0;
    }
    let (cos, sin) = (theta.cos(), theta.sin());
    let half_chord = (1.0 - y * y).sqrt() * cos.abs();
    let offset = THICKNESS / 2.0 * sin;
    let face_x = if cos > 0.0 { offset } else { -offset }; // centre of whichever face points at the viewer
    if cos.abs() > 1e-3 && (x - face_x).abs() <= half_chord {
        let u = (x - face_x) / cos.abs();
        let ring = u * u + y * y >= 0.80 * 0.80;
        let glyph = if cos > 0.0 { in_f(u, y) } else { in_bolt(u, y) };
        return (ring || glyph) as u8;
    }
    // The rim is dithered so it reads as a different surface.
    if x.abs() <= offset.abs() + half_chord && parity { 2 } else { 0 }
}

fn shade(rgb: (f64, f64, f64), k: f64) -> (u8, u8, u8) {
    ((rgb.0 * k) as u8, (rgb.1 * k) as u8, (rgb.2 * k) as u8)
}

/// The coin at rotation `theta` (0 shows the F, pi shows the bolt): ROWS lines of coloured Braille.
pub fn frame(theta: f64) -> Vec<String> {
    let size = ROWS * 4;
    let scale = size as f64 / 2.0 / 1.06;
    let (cos, sin) = (theta.cos(), theta.sin());
    let face = shade(if cos > 0.0 { F_FACE } else { BOLT_FACE }, 0.4 + 0.6 * cos.abs());
    let rim = shade(RIM, 0.55 + 0.45 * sin.abs());
    let mut lines = Vec::new();
    for row in 0..ROWS {
        let mut line = String::new();
        for col in 0..size / 2 {
            let (mut bits, mut lit, mut on_rim) = (0u32, 0, 0);
            for (dy, dots) in DOTS.iter().enumerate() {
                for (dx, bit) in dots.iter().enumerate() {
                    let (px, py) = (col * 2 + dx, row * 4 + dy);
                    let kind = sample((px as f64 + 0.5 - size as f64 / 2.0) / scale, (py as f64 + 0.5 - size as f64 / 2.0) / scale, theta, (px + py) % 2 == 1);
                    if kind > 0 {
                        bits |= bit;
                        lit += 1;
                        on_rim += (kind == 2) as u32;
                    }
                }
            }
            let (r, g, b) = if on_rim * 2 > lit { rim } else { face };
            let dot = char::from_u32(0x2800 + bits).unwrap_or(' ');
            line.push_str(&format!("\x1b[38;2;{r};{g};{b}m{dot}"));
        }
        line.push_str("\x1b[0m");
        lines.push(line);
    }
    lines
}

/// Flip the coin and let it settle on the F, with `beside` (one string per row) to its right.
pub fn spin(beside: &[String], seconds: f64) {
    let draw = |theta: f64| {
        let mut out = String::new();
        for (i, line) in frame(theta).iter().enumerate() {
            out.push_str(&format!("  {line}   {}\x1b[K\n", beside.get(i).map(String::as_str).unwrap_or("")));
        }
        out
    };
    let mut stdout = std::io::stdout();
    let animate = seconds > 0.0 && crossterm::tty::IsTty::is_tty(&stdout);
    if animate {
        let start = std::time::Instant::now();
        let _ = write!(stdout, "\x1b[?25l{}", draw(0.0));
        loop {
            let t = start.elapsed().as_secs_f64() / seconds;
            if t >= 1.0 {
                break;
            }
            let eased = 1.0 - (1.0 - t).powi(3); // fast, then settling
            let _ = write!(stdout, "\x1b[{ROWS}A{}", draw(2.0 * 2.0 * PI * eased));
            let _ = stdout.flush();
            std::thread::sleep(std::time::Duration::from_millis(33));
        }
        let _ = write!(stdout, "\x1b[{ROWS}A{}\x1b[?25h", draw(0.0));
    } else {
        let _ = write!(stdout, "{}", draw(0.0));
    }
    let _ = stdout.flush();
}

/// Show a custom logo (lines of coloured text) beside `beside`, drawn in top to bottom.
pub fn reveal(logo: &[String], beside: &[String], animate: bool) {
    let rows = logo.len().max(beside.len());
    let width = logo.iter().map(|l| visible_width(l)).max().unwrap_or(0);
    let animate = animate && crossterm::tty::IsTty::is_tty(&std::io::stdout());
    let mut stdout = std::io::stdout();
    for i in 0..rows {
        let art = logo.get(i).map(String::as_str).unwrap_or("");
        let pad = " ".repeat(width - visible_width(art));
        let _ = writeln!(stdout, "  {art}\x1b[0m{pad}   {}", beside.get(i).map(String::as_str).unwrap_or(""));
        if animate {
            let _ = stdout.flush();
            std::thread::sleep(std::time::Duration::from_millis(28));
        }
    }
}

/// Width on screen: escape sequences take none.
fn visible_width(text: &str) -> usize {
    let mut width = 0;
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            chars.by_ref().find(|c| c.is_ascii_alphabetic());
        } else {
            width += 1;
        }
    }
    width
}

/// An image as terminal art, `rows` lines tall: each cell is two pixels, top and bottom, drawn
/// with a half block in true colour. Transparent pixels stay transparent.
pub fn image_to_art(path: &std::path::Path, rows: u32) -> anyhow::Result<Vec<String>> {
    let img = image::open(path)?.to_rgba8();
    let (w, h) = img.dimensions();
    let max_px_h = rows * 2;
    let max_w = rows * 3; // keep wide logos beside the banner
    let scale = (max_px_h as f64 / h as f64).min(max_w as f64 / w as f64);
    let (nw, nh) = (((w as f64 * scale).round() as u32).max(1), ((h as f64 * scale).round() as u32).max(1));
    let img = image::imageops::resize(&img, nw, nh, image::imageops::FilterType::Lanczos3);
    let mut lines = Vec::new();
    for y in (0..nh).step_by(2) {
        let mut line = String::new();
        for x in 0..nw {
            let top = img.get_pixel(x, y);
            let bottom = if y + 1 < nh { *img.get_pixel(x, y + 1) } else { image::Rgba([0, 0, 0, 0]) };
            let (t, b) = (top[3] >= 128, bottom[3] >= 128);
            line.push_str(&match (t, b) {
                (true, true) => format!("\x1b[38;2;{};{};{}m\x1b[48;2;{};{};{}m▀\x1b[0m", top[0], top[1], top[2], bottom[0], bottom[1], bottom[2]),
                (true, false) => format!("\x1b[38;2;{};{};{}m▀\x1b[0m", top[0], top[1], top[2]),
                (false, true) => format!("\x1b[38;2;{};{};{}m▄\x1b[0m", bottom[0], bottom[1], bottom[2]),
                (false, false) => " ".into(),
            });
        }
        lines.push(line);
    }
    Ok(lines)
}
