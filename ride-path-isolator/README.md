# Ride Path Isolator

A single-file static website that takes a map screenshot of a ride (Google Maps
timeline, ride tracker, etc.) where the route is drawn as a speed-colored line, and
produces an image with **only the path** on a clean white background — speed colors
preserved.

## Use

Open `index.html` in any browser (or host it as a static page). Then:

1. Drop a screenshot in, pick a file, or paste from the clipboard.
2. The path is isolated automatically; tweak with the toolbar if needed:
   - **Color sensitivity** — how saturated a pixel must be to count as path.
   - **Remove specks** — drops small stray blobs (text fragments, noise).
   - **Map filter** — cuts pink/purple POI labels and washed-out cyan water edges.
3. Click any leftover blob (legend bar, markers, logos) in the result to erase it;
   **Undo erase** brings it back.
4. **Download PNG** — optionally trimmed to the path's bounding box.

Everything runs client-side; the image never leaves the browser.

## How it works

- Pixels are classified by HSV saturation/hue: the map background (pale water, gray
  roads, white labels) is washed out, while the route line is vivid.
- Detection is two-pass: strongly-saturated pixels seed the mask, which then grows a
  bounded number of steps through weaker colorful pixels. Real path segments always
  have a vivid core; water fringes and shoreline gradients don't, so they are dropped
  even when they clear the weak threshold.
- Remaining blobs are 4-connected components; tiny ones are removed automatically and
  any component can be erased with a click.
