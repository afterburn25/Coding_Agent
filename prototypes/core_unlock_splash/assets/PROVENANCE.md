# Artwork and geometry

`nexus-core-splash.png` is an unmodified copy of the production artwork at
`desktop/ChatNexus.Desktop/nexus-core-splash.png`, taken from main commit
`a45c24cfe6f5b21c2cb8a78bce5aacc21377cfd5`.

SHA-256: `46e5f31dbe656cf85b3052c05015138e828b43a5b06127f8ebc8814d81bced0c`.
Its existing repository ownership/permissions remain unchanged.

The October 5 review videos intentionally retain this approved source image.
Production main subsequently adopted a composited locked opening frame
(`813f230`). Do not replace the prototype background with that composite: its
mechanism would be drawn again underneath the procedural renderer.

The supplied larger original and locked/online reference images informed the
mechanism and lighting design only. They are not animation frames or runtime assets.
No image generation or image crossfading is used.

All new mechanical art is editable deterministic vector geometry in
`web/renderer.mjs`: three ring layers, six rigid iris leaves, four clamps, one
key cylinder/carriage, a sphere, orbital structures and cached glow sprites.
Mechanical and light sprites are rasterized once in memory at 2× resolution.
The plasma and blue chamber haze are original deterministic noise fields,
cached at 256×256 and 384×384 respectively. Platform emission, floor reflection,
orbital glow, metal bevels and recessed shadows are editable in the same renderer.
The stronger lighting follows charge intensity and stays active during readiness
holds. No source image is edited or replaced. No fonts, textures,
CDNs or remote assets are downloaded. Production lettering stays in the original
image, so it cannot change shape during animation.

Coordinate system: 1024×576; core center (512, 204); housing radius 94; cavity
radius 64. The unchanged shield remains visible around the mechanism. The
existing painted progress region is covered with a local opaque readout, as in
the production splash. Its fill is explicitly preview timing until the host
supplies real progress through `setProgress`.
