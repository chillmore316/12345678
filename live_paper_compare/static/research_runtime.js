'use strict';

const runtimePrepareButton = document.getElementById('runtime-prepare');
const runtimePrepareStatus = document.getElementById('runtime-prepare-status');
runtimePrepareButton.addEventListener('click', async () => {
  runtimePrepareButton.disabled = true;
  runtimePrepareStatus.textContent = '正在检查专用虚拟机和离线执行环境…';
  try {
    const response = await fetch('/api/research/runtime/prepare', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}',
      mode: 'same-origin',
      redirect: 'error',
      credentials: 'omit',
      cache: 'no-store',
    });
    const result = await response.json();
    if (response.ok && result.prepared === true) {
      runtimePrepareStatus.textContent = result.recovered_owned_job
        ? '已清理上次中断留下的研究任务。隔离环境已就绪，可继续原操作。'
        : '隔离环境已就绪。请手动提交回测或继续历史演练。';
    } else {
      runtimePrepareStatus.textContent = result.error || '隔离环境暂未就绪，请稍后重试。';
    }
  } catch {
    runtimePrepareStatus.textContent = '本地服务响应中断，请检查服务后重新准备隔离环境。';
  } finally {
    runtimePrepareButton.disabled = false;
  }
});
