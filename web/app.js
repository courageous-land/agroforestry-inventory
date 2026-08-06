// Viewer: the orthophoto and the detections on a map.
//
// No API key and no external service - the map library lives in this repository
// and the tiles come from the local server, cut from the COG on request.

const $ = (id) => document.getElementById(id);

const state = {
  info: null,
  detections: null,
  minConfidence: 0.25,
  boxes: true,
  centres: false,
};

function notice(text, isError = false) {
  const el = $('notice');
  el.textContent = text;
  el.hidden = !text;
  el.classList.toggle('error', isError);
  if (text && !isError) setTimeout(() => { el.hidden = true; }, 3500);
}

// ---------------------------------------------------------------------- map
function createMap(info) {
  const [west, south, east, north] = info.bounds_4326;

  const map = new maplibregl.Map({
    container: 'map',
    // minimal style: no remote basemap, so the page works offline
    style: {
      version: 8,
      sources: {},
      layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#0b1220' } }],
    },
    bounds: [[west, south], [east, north]],
    fitBoundsOptions: { padding: { top: 40, bottom: 40, left: 340, right: 40 } },
    maxZoom: 25,
    dragRotate: false,
    attributionControl: { compact: true },
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 140, unit: 'metric' }), 'bottom-left');

  return new Promise((resolve) => {
    map.on('load', () => {
      map.addSource('ortho', {
        type: 'raster',
        // Two modes, one codebase. With the local server, each tile is cut from
        // the COG on request. In a static publication the tiles are already on
        // disk and `tiles_url` points at them - which is what allows hosting the
        // demo with no server at all.
        tiles: [info.tiles_url || `${location.origin}/cog/{z}/{x}/{y}.png`],
        tileSize: 256,
        // TMS axis convention, and minzoom/maxzoom/bounds read from the file
        // itself - without them the map would request tiles for the whole world
        scheme: info.scheme || 'tms',
        minzoom: info.minzoom,
        maxzoom: info.maxzoom,
        bounds: info.bounds_4326,
      });
      map.addLayer({ id: 'ortho', type: 'raster', source: 'ortho', paint: { 'raster-opacity': 1 } });
      resolve(map);
    });
  });
}

// --------------------------------------------------------------- detections
function addDetections(map, geojson) {
  map.addSource('det', { type: 'geojson', data: geojson });

  map.addLayer({
    id: 'det-fill',
    type: 'fill',
    source: 'det',
    paint: { 'fill-color': '#f97316', 'fill-opacity': 0.12 },
  });
  map.addLayer({
    id: 'det-line',
    type: 'line',
    source: 'det',
    paint: { 'line-color': '#f97316', 'line-width': 2.5, 'line-opacity': 1 },
  });
  // a dot at the centre of each box: at wide zoom the box disappears, the dot does not
  map.addLayer({
    id: 'det-centre',
    type: 'circle',
    source: 'det',
    layout: { visibility: 'none' },
    paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 14, 1.5, 20, 4, 23, 7],
      'circle-color': '#fde047',
      'circle-stroke-width': 0.5,
      'circle-stroke-color': '#78350f',
    },
  });

  map.on('click', 'det-fill', (e) => {
    const f = e.features && e.features[0];
    if (!f) return;
    new maplibregl.Popup({ closeButton: false })
      .setLngLat(e.lngLat)
      .setHTML(
        `<strong>${f.properties.class ?? '—'}</strong><br>` +
        `confidence ${Number(f.properties.confidence ?? 0).toFixed(2)}`,
      )
      .addTo(map);
  });
  map.on('mouseenter', 'det-fill', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'det-fill', () => { map.getCanvas().style.cursor = ''; });
}

function applyFilter(map) {
  const filter = ['>=', ['get', 'confidence'], state.minConfidence];
  for (const id of ['det-fill', 'det-line', 'det-centre']) {
    if (map.getLayer(id)) map.setFilter(id, filter);
  }
  const n = (state.detections?.features || [])
    .filter((f) => (f.properties?.confidence ?? 0) >= state.minConfidence).length;
  $('total').textContent = n.toLocaleString('en-US');
}

// ----------------------------------------------------------------- controls
function installControls(map) {
  $('conf').addEventListener('input', (e) => {
    state.minConfidence = Number(e.target.value);
    $('conf-value').textContent = state.minConfidence.toFixed(2);
    applyFilter(map);
  });

  $('opacity').addEventListener('input', (e) => {
    $('opacity-value').textContent = Math.round(Number(e.target.value) * 100);
    map.setPaintProperty('ortho', 'raster-opacity', Number(e.target.value));
  });

  const toggle = (button, layers, key) => {
    button.addEventListener('click', () => {
      state[key] = !state[key];
      button.classList.toggle('on', state[key]);
      for (const id of layers) {
        if (map.getLayer(id)) {
          map.setLayoutProperty(id, 'visibility', state[key] ? 'visible' : 'none');
        }
      }
    });
  };
  toggle($('btn-boxes'), ['det-fill', 'det-line'], 'boxes');
  toggle($('btn-centres'), ['det-centre'], 'centres');

  $('btn-export').addEventListener('click', () => {
    const features = (state.detections?.features || [])
      .filter((f) => (f.properties?.confidence ?? 0) >= state.minConfidence);
    const blob = new Blob(
      [JSON.stringify({ type: 'FeatureCollection', features }, null, 1)],
      { type: 'application/geo+json' },
    );
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `detections_conf${state.minConfidence.toFixed(2)}.geojson`;
    a.click();
    URL.revokeObjectURL(a.href);
    notice(`${features.length} detections exported`);
  });
}

// --------------------------------------------------------------- model card
function showCard(m) {
  if (!m) {
    $('card').hidden = true;
    return;
  }
  $('species').textContent = m.title || m.species || 'detections';
  $('site').textContent = m.site ? `model trained at ${m.site}` : '';

  const row = (label, value) => `<div class="pair"><span>${label}</span><strong>${value}</strong></div>`;
  $('card-body').innerHTML =
    row('precision', m.precision ?? '—') +
    row('recall', m.recall ?? '—') +
    row('position error', m.position_error_m != null ? `${m.position_error_m} m` : '—') +
    row('median crown', m.median_crown_m != null ? `${m.median_crown_m} m` : '—') +
    (m.where_it_fails ? `<p class="caveat"><strong>Where it fails:</strong> ${m.where_it_fails}</p>` : '') +
    `<p class="caveat">These numbers hold for <em>${m.site || 'the training site'}</em>.
      Crown detection models do not transfer between places without loss - check a
      sample by hand before trusting the count.</p>`;
}

// -------------------------------------------------------------------- start
// With a server, the data comes from the API. In a static publication it is
// files next to the page. Try the API and fall back to the files.
async function load(apiRoute, file) {
  try {
    const r = await fetch(apiRoute);
    if (r.ok) return await r.json();
  } catch { /* no server: fall through to the file */ }
  return (await fetch(file)).json();
}

(async function start() {
  try {
    const info = await load('/api/info', './info.json');
    state.info = info;
    const map = await createMap(info);

    const geojson = await load('/api/detections', './detections.geojson');
    state.detections = geojson;
    if ((geojson.features || []).length) {
      addDetections(map, geojson);
    } else {
      notice('No detections to show - orthophoto only.', true);
    }

    showCard(info.model);
    installControls(map);
    applyFilter(map);
    window.__inventory = { map, state };
  } catch (e) {
    console.error(e);
    notice(`Failed to start: ${e.message}`, true);
  }
})();
