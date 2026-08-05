// Visualizador: a ortofoto e as detecções sobre um mapa.
//
// Sem chave de API e sem serviço externo — a biblioteca de mapa está no próprio
// repositório e os tiles vêm do servidor local, recortados do COG na hora.

const $ = (id) => document.getElementById(id);

const estado = {
  info: null,
  deteccoes: null,
  confMin: 0.25,
  caixas: true,
  centros: false,
};

function aviso(texto, erro = false) {
  const el = $('aviso');
  el.textContent = texto;
  el.hidden = !texto;
  el.classList.toggle('erro', erro);
  if (texto && !erro) setTimeout(() => { el.hidden = true; }, 3500);
}

// --------------------------------------------------------------------- mapa
function criarMapa(info) {
  const [oeste, sul, leste, norte] = info.bounds_4326;

  const mapa = new maplibregl.Map({
    container: 'mapa',
    // estilo mínimo: sem basemap remoto, o app funciona offline
    style: {
      version: 8,
      sources: {},
      layers: [{ id: 'fundo', type: 'background', paint: { 'background-color': '#0b1220' } }],
    },
    bounds: [[oeste, sul], [leste, norte]],
    fitBoundsOptions: { padding: { top: 40, bottom: 40, left: 340, right: 40 } },
    maxZoom: 25,
    dragRotate: false,
    attributionControl: { compact: true },
  });
  mapa.touchZoomRotate.disableRotation();
  mapa.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');
  mapa.addControl(new maplibregl.ScaleControl({ maxWidth: 140, unit: 'metric' }), 'bottom-left');

  return new Promise((resolve) => {
    mapa.on('load', () => {
      mapa.addSource('orto', {
        type: 'raster',
        tiles: [`${location.origin}/cog/{z}/{x}/{y}.png`],
        tileSize: 256,
        // o servidor entrega no eixo TMS, e minzoom,
        // maxzoom e bounds vêm do próprio arquivo — sem eles o mapa pediria
        // tiles do mundo inteiro
        scheme: info.esquema || 'tms',
        minzoom: info.minzoom,
        maxzoom: info.maxzoom,
        bounds: info.bounds_4326,
      });
      mapa.addLayer({ id: 'orto', type: 'raster', source: 'orto', paint: { 'raster-opacity': 1 } });
      resolve(mapa);
    });
  });
}

// --------------------------------------------------------------- detecções
function adicionarDeteccoes(mapa, geojson) {
  mapa.addSource('det', { type: 'geojson', data: geojson });

  mapa.addLayer({
    id: 'det-preenchimento',
    type: 'fill',
    source: 'det',
    paint: { 'fill-color': '#f97316', 'fill-opacity': 0.12 },
  });
  mapa.addLayer({
    id: 'det-linha',
    type: 'line',
    source: 'det',
    paint: { 'line-color': '#f97316', 'line-width': 2.5, 'line-opacity': 1 },
  });
  // um ponto no centro de cada caixa: em zoom baixo a caixa some, o ponto não
  mapa.addLayer({
    id: 'det-centro',
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

  // clicar numa detecção mostra classe e confiança
  mapa.on('click', 'det-preenchimento', (e) => {
    const f = e.features && e.features[0];
    if (!f) return;
    new maplibregl.Popup({ closeButton: false })
      .setLngLat(e.lngLat)
      .setHTML(
        `<strong>${f.properties.classe ?? '—'}</strong><br>` +
        `confiança ${Number(f.properties.confianca ?? 0).toFixed(2)}`,
      )
      .addTo(mapa);
  });
  mapa.on('mouseenter', 'det-preenchimento', () => { mapa.getCanvas().style.cursor = 'pointer'; });
  mapa.on('mouseleave', 'det-preenchimento', () => { mapa.getCanvas().style.cursor = ''; });
}

function aplicarFiltro(mapa) {
  const filtro = ['>=', ['get', 'confianca'], estado.confMin];
  for (const id of ['det-preenchimento', 'det-linha', 'det-centro']) {
    if (mapa.getLayer(id)) mapa.setFilter(id, filtro);
  }
  const n = (estado.deteccoes?.features || [])
    .filter((f) => (f.properties?.confianca ?? 0) >= estado.confMin).length;
  $('contagem').textContent = n.toLocaleString('pt-BR');
}

// ---------------------------------------------------------------- controles
function instalarControles(mapa) {
  $('conf').addEventListener('input', (e) => {
    estado.confMin = Number(e.target.value);
    $('conf-valor').textContent = estado.confMin.toFixed(2).replace('.', ',');
    aplicarFiltro(mapa);
  });

  $('opac').addEventListener('input', (e) => {
    $('opac-valor').textContent = Math.round(Number(e.target.value) * 100);
    mapa.setPaintProperty('orto', 'raster-opacity', Number(e.target.value));
  });

  const alternar = (botao, camadas, chave) => {
    botao.addEventListener('click', () => {
      estado[chave] = !estado[chave];
      botao.classList.toggle('ligado', estado[chave]);
      for (const id of camadas) {
        if (mapa.getLayer(id)) {
          mapa.setLayoutProperty(id, 'visibility', estado[chave] ? 'visible' : 'none');
        }
      }
    });
  };
  alternar($('btn-caixas'), ['det-preenchimento', 'det-linha'], 'caixas');
  alternar($('btn-centro'), ['det-centro'], 'centros');

  $('btn-exportar').addEventListener('click', () => {
    const feicoes = (estado.deteccoes?.features || [])
      .filter((f) => (f.properties?.confianca ?? 0) >= estado.confMin);
    const blob = new Blob(
      [JSON.stringify({ type: 'FeatureCollection', features: feicoes }, null, 1)],
      { type: 'application/geo+json' },
    );
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `deteccoes_conf${estado.confMin.toFixed(2)}.geojson`;
    a.click();
    URL.revokeObjectURL(a.href);
    aviso(`${feicoes.length} detecções exportadas`);
  });
}

// ------------------------------------------------------------- ficha modelo
function mostrarFicha(m) {
  if (!m) {
    $('ficha').hidden = true;
    return;
  }
  $('especie').textContent = m.titulo || m.especie || 'detecções';
  $('sitio').textContent = m.sitio ? `modelo treinado em ${m.sitio}` : '';

  const linha = (r, v) => `<div class="par"><span>${r}</span><strong>${v}</strong></div>`;
  const br = (x) => String(x).replace('.', ',');
  $('ficha-corpo').innerHTML =
    linha('precisão', br(m.precisao ?? '—')) +
    linha('recall', br(m.recall ?? '—')) +
    linha('erro de posição', m.erro_de_posicao_m != null ? `${br(m.erro_de_posicao_m)} m` : '—') +
    linha('copa mediana', m.copa_mediana_m != null ? `${br(m.copa_mediana_m)} m` : '—') +
    (m.onde_falha ? `<p class="falha"><strong>Onde falha:</strong> ${m.onde_falha}</p>` : '') +
    `<p class="falha">Estes números valem para <em>${m.sitio || 'o sítio de treino'}</em>.
      Modelos de detecção de copa não transferem entre lugares sem perda — confira
      uma amostra à mão antes de confiar na contagem.</p>`;
}

// -------------------------------------------------------------------- início
(async function iniciar() {
  try {
    const info = await (await fetch('/api/info')).json();
    estado.info = info;
    const mapa = await criarMapa(info);

    const geojson = await (await fetch('/api/deteccoes')).json();
    estado.deteccoes = geojson;
    if ((geojson.features || []).length) {
      adicionarDeteccoes(mapa, geojson);
    } else {
      aviso('Nenhuma detecção para mostrar — só a ortofoto.', true);
    }

    mostrarFicha(info.modelo);
    instalarControles(mapa);
    aplicarFiltro(mapa);
    window.__inventario = { mapa, estado };
  } catch (e) {
    console.error(e);
    aviso(`Falha ao iniciar: ${e.message}`, true);
  }
})();
