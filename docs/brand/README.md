# myflames app mark

`myflames-icon-v3.png` keeps the five flame segments, with saturated yellow,
orange and red on white. It is the current desktop, workspace and favicon mark.
`myflames-icon-v2.png` is preserved for earlier references and the project README.
`myflames-icon-v3-128.png` is the same mark scaled to 128 px (macOS `sips -Z 128`) for the
workspace sidebar, About and favicon, so the app does not decode a 1254 px image to draw it at 22 px.
The app renders its name as live text beside the icon. The original
`myflames.jpeg` stays in the repository root.

Created with the built-in image-generation tool, using `myflames.jpeg` as the
reference. The generated PNG is preserved unchanged, including its alpha channel.

## Generation prompt

Use case: logo-brand. Asset type: production app icon for myflames, a MySQL/MariaDB query-plan visualization tool. Redesign the attached existing myflames logo into a restrained, professional icon suitable for a polished macOS-style application. Preserve its distinctive idea: stacked horizontal flame-graph bars forming one flame silhouette. Simplify to five broad horizontal segments with generous even negative-space gaps, softly rounded ends, an asymmetrical rising flame tip, and a rounded base. Use warm burnt orange at the base transitioning subtly to golden orange at the tip, with enough contrast on a very light neutral background. Flat vector-like geometry, exceptionally crisp edges, balanced optical weight. Large central mark fills about 76 percent of the square canvas, generous equal margins. Plain near-white background #f5f5f7, no surrounding tile outline. Remove all text, wordmark, dolphins and seals for legibility at 32–48 pixels; the app displays its name separately. No 3D, no shadows, no gloss, no mockup, no letters, no extra elements. Deliver one finished square icon, not a presentation sheet.

## Asset delivery

The UI build emits the icon at `/assets/myflames-logo.png`, a stable address
shared by the sidebar, welcome image, and favicon. Keep this filename stable
across builds. The server also serves this icon for the earlier hashed logo
URLs so existing tabs do not encounter a missing image after an update.
The favicon link adds `?v=3` to refresh browsers' separate tab-icon caches.
Increment this version when replacing the app mark; retain the stable image path.
