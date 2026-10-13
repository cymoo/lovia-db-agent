// Components for results too big to send through the model: `db.table` and
// `db.chart`. The tool output carries a preview (or a summary) and a
// `data_url`; these renderers fetch the full rows from the app's
// /ext/results endpoint and hand them to lovia's built-in `table` / `chart`
// renderers. If the cached result has expired, the table falls back to its
// preview and the chart says so.

const table = window.lovia.getRenderer('table');
const chart = window.lovia.getRenderer('chart');

const LABELS = {
  en: {
    expired: 'The full result has expired — showing the first rows. Ask again to rerun the query.',
    chartExpired: 'This chart’s data has expired. Ask again to rerun the query.',
    truncated: (n) => `Showing the first ${n} rows; the query returned more.`,
  },
  zh: {
    expired: '完整结果已过期，这里只显示前几行。可以让助手重新查询。',
    chartExpired: '图表数据已过期，可以让助手重新查询。',
    truncated: (n) => `仅显示前 ${n} 行，查询结果还有更多。`,
  },
};

// Components re-render on a theme switch: fetch each result once.
const loads = new Map();

/** @returns {Promise<{columns: string[], rows: unknown[][]} | null>} */
function load(ctx, url) {
  const href = `${ctx.basePath}/${url}`;
  if (!loads.has(href)) {
    loads.set(
      href,
      fetch(href, { credentials: 'same-origin' })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => {
          loads.delete(href); // a network blip may pass; let a re-render retry
          return null;
        }),
    );
  }
  return loads.get(href);
}

function note(el, text) {
  const div = document.createElement('div');
  div.className = 'tool-ui-notice';
  div.style.marginTop = '6px';
  div.textContent = text;
  el.append(div);
}

window.lovia.registerRenderer('db.table', async (el, props, ctx) => {
  const L = LABELS[ctx.lang] || LABELS.en;
  const data = typeof props?.data_url === 'string' ? await load(ctx, props.data_url) : null;
  const cleanup = await table(el, data ? { ...props, rows: data.rows } : props, ctx);
  if (!data) note(el, L.expired);
  else if (props.truncated) note(el, L.truncated(data.rows.length.toLocaleString()));
  return cleanup;
});

window.lovia.registerRenderer('db.chart', async (el, props, ctx) => {
  const L = LABELS[ctx.lang] || LABELS.en;
  const data = typeof props?.data_url === 'string' ? await load(ctx, props.data_url) : null;
  if (!data) {
    note(el, L.chartExpired);
    return undefined;
  }
  return chart(el, { ...props.option, dataset: { source: [data.columns, ...data.rows] } }, ctx);
});
