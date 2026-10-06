document.addEventListener('DOMContentLoaded', () => {
  const fileInput = document.querySelector('#feedback-file');
  const fileName = document.querySelector('#file-name');
  if (fileInput && fileName) {
    fileInput.addEventListener('change', () => {
      fileName.textContent = fileInput.files[0]?.name || 'Choose a CSV file';
    });
  }

  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const menu = document.querySelector('#mobile-menu');
  const sidebar = document.querySelector('#sidebar');
  if (menu && sidebar) {
    menu.addEventListener('click', () => sidebar.classList.toggle('mobile-open'));
  }

  if (!window.Chart) return;
  const chartDefaults = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false }, tooltip: { backgroundColor: '#172c43', padding: 10, titleFont: { family: 'DM Sans' }, bodyFont: { family: 'DM Sans' } } }
  };
  const colors = { positive: '#4da779', neutral: '#e3ad4f', negative: '#df746b', navy: '#1a6c50', grid: '#edf1f4' };

  const dashboardDataNode = document.querySelector('#dashboard-chart-data');
  if (dashboardDataNode) {
    const data = JSON.parse(dashboardDataNode.textContent);
    const sentiment = document.querySelector('#sentiment-chart');
    if (sentiment) new Chart(sentiment, {
      type: 'doughnut',
      data: { labels: ['Positive', 'Neutral', 'Negative'], datasets: [{ data: [data.counts.positive, data.counts.neutral, data.counts.negative], backgroundColor: [colors.positive, colors.neutral, colors.negative], borderWidth: 0, hoverOffset: 5 }] },
      options: { ...chartDefaults, cutout: '75%', plugins: { ...chartDefaults.plugins, tooltip: { ...chartDefaults.plugins.tooltip, callbacks: { label: (context) => ` ${context.label}: ${context.raw}` } } } }
    });
    const trend = document.querySelector('#trend-chart');
    if (trend) new Chart(trend, {
      type: 'line',
      data: { labels: data.trend.labels, datasets: [
        { label: 'Positive', data: data.trend.positive, borderColor: colors.positive, backgroundColor: colors.positive, tension: .35, pointRadius: 3, borderWidth: 2 },
        { label: 'Neutral', data: data.trend.neutral, borderColor: colors.neutral, backgroundColor: colors.neutral, tension: .35, pointRadius: 3, borderWidth: 2 },
        { label: 'Negative', data: data.trend.negative, borderColor: colors.negative, backgroundColor: colors.negative, tension: .35, pointRadius: 3, borderWidth: 2 }
      ] },
      options: { ...chartDefaults, scales: { x: { grid: { display: false }, ticks: { color: '#87949f', font: { family: 'DM Sans', size: 9 } }, border: { display: false } }, y: { beginAtZero: true, grid: { color: colors.grid }, ticks: { precision: 0, color: '#87949f', font: { family: 'DM Sans', size: 9 } }, border: { display: false } } } }
    });
    const categories = document.querySelector('#category-chart');
    if (categories) {
      const rows = data.categories;
      new Chart(categories, {
        type: 'bar',
        data: { labels: rows.map((row) => row.category), datasets: [{ label: 'Reviews', data: rows.map((row) => row.total), backgroundColor: rows.map((row) => row.negative ? colors.negative : colors.navy), borderRadius: 4, barThickness: 13 }] },
        options: { ...chartDefaults, indexAxis: 'y', scales: { x: { beginAtZero: true, grid: { color: colors.grid }, ticks: { precision: 0, color: '#87949f', font: { family: 'DM Sans', size: 9 } }, border: { display: false } }, y: { grid: { display: false }, ticks: { color: '#566777', font: { family: 'DM Sans', size: 10 } }, border: { display: false } } } }
      });
    }
  }

  const reportDataNode = document.querySelector('#report-chart-data');
  const reportCanvas = document.querySelector('#report-trend-chart');
  if (reportDataNode && reportCanvas) {
    const data = JSON.parse(reportDataNode.textContent);
    new Chart(reportCanvas, {
      type: 'bar',
      data: { labels: data.trend.labels, datasets: [
        { label: 'Positive', data: data.trend.positive, backgroundColor: colors.positive, borderRadius: 3 },
        { label: 'Neutral', data: data.trend.neutral, backgroundColor: colors.neutral, borderRadius: 3 },
        { label: 'Negative', data: data.trend.negative, backgroundColor: colors.negative, borderRadius: 3 }
      ] },
      options: { ...chartDefaults, scales: { x: { stacked: true, grid: { display: false }, ticks: { color: '#87949f', font: { family: 'DM Sans', size: 9 } }, border: { display: false } }, y: { stacked: true, beginAtZero: true, grid: { color: colors.grid }, ticks: { precision: 0, color: '#87949f', font: { family: 'DM Sans', size: 9 } }, border: { display: false } } } }
    });
  }
});
