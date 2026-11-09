# myflames brand

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="myflames-wordmark-on-dark.svg">
  <img src="myflames-wordmark-on-light.svg" alt="myflames" width="343" height="70">
</picture>

**See what your database is doing.**

myflames is an open-source performance workspace for MySQL, MariaDB, and Percona Server for MySQL. Use this description and message across the repository, website, documentation, and project material.

## Logo and wordmark

The primary mark is a dual-blade woven flame: a tall tangerine spire overlapped by a warm golden-amber ribbon fold. Use the lowercase `myflames` wordmark alongside it when space allows. Keep Ember, the mascot, as a companion illustration rather than substituting it for the logo.

| Asset | Use |
| :--- | :--- |
| [myflames-app-icon.icns](myflames-app-icon.icns) | macOS desktop app icon bundle (16–512 pt at 1× and 2× on an anodized charcoal-slate squircle). |
| [myflames-app-icon.png](myflames-app-icon.png) | Desktop icon master, 1024 × 1024 with transparent margins, used for Windows packaging and previews. |
| [myflames-app-icon.svg](myflames-app-icon.svg) | Editable desktop icon master (`1024 × 1024` Apple continuous superellipse squircle). |
| [myflames-logo.svg](myflames-logo.svg) | Primary flame mark (`64 × 72`), transparent background. |
| [myflames-wordmark-on-light.svg](myflames-wordmark-on-light.svg) | Flame and dark wordmark for light backgrounds. |
| [myflames-wordmark-on-dark.svg](myflames-wordmark-on-dark.svg) | Flame and warm white wordmark for dark backgrounds. |
| [myflames-mascot.png](myflames-mascot.png) | Ember, the friendly flame mascot for welcomes, learning, and community material. |
| [social-preview.png](social-preview.png) | Repository social preview, 1280 × 640 pixels. |
| [social-preview.svg](social-preview.svg) | Editable source for the social preview. |

Keep the mark's proportions and colors. Leave clear space of at least one quarter of its width on each side. Keep it separate from surrounding text and other logos. Use the appropriate wordmark for the background; the standalone flame works on light and dark surfaces.

The palette matches the separate website: flame `#FF873F`, inner glow `#FFE2A4`, charcoal `#141B1B`, and warm white `#F4F2E9`. Use a system sans-serif font for accompanying text.

## Mascot: Ember

<img src="myflames-mascot.png" alt="Ember, a round golden-orange flame with a flame-shaped crest, kind eyes, and a welcoming wave" width="240">

Ember brings warmth to learning and community material. Preserve Ember's round body, upright flame crest, warm orange and gold colors, and kind expression.

## Earlier app assets

Existing released installers retain their original artwork; the earlier segmented marks and the original `myflames.jpeg` were removed from the repository. The workspace uses the primary flame; desktop packages use the charcoal-slate app icon.

The workspace build emits `myflames-app-logo.png`, rasterized from the primary SVG, at `/assets/myflames-logo.png`, shared by the sidebar, welcome image, and favicon. The desktop icon uses the same flame geometry as the website and workspace; its editable master is `myflames-app-icon.svg`. Windows packages use the PNG master. On macOS, standard 16, 32, 128, 256, and 512 point representations at 1× and 2× are assembled with `iconutil -c icns` into `myflames-app-icon.icns`.

The SVG logo and wordmarks are original editable paths under the repository's Apache-2.0 license. The mascot was generated and refined with Krea GPT Image 2, with the original image preserved by the separate website project.
