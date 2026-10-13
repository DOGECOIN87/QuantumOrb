# Decision 0004: Frontend source location

The public repository does not contain the runnable Observatory frontend, but its sealed manifest records the frontend as part of the closed source:

- `site/client/core.js`
- `site/client/main.js`
- `site/client/math.js`
- `site/client/pons.js`
- `site/public/css/app.css`
- `site/public/js/app.js`
- `site/public/js/fit.js`
- `site/server/views/index.html`
- `site/server/render/home.js`
- `site/server/render/live.js`

The manifest also records `site/package.json`, the build scripts, and the Observatory server. These are commitments in the seal, not recoverable source files. No separate public ghost repository was found.

For a personal themed frontend, implement a new frontend against the documented API/domain contracts rather than attempting to reconstruct the sealed files. Keep the new UI in a separate private application directory and treat the sealed frontend only as a visual/architectural reference.
