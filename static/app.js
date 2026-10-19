/* Lazarus Alpine.js Application Store & Helpers */

document.addEventListener('alpine:init', () => {
  Alpine.data('lazarusApp', () => ({
    activeTab: window.location.hash ? window.location.hash.substring(1) : 'overview',
    connected: false,
    lastRefresh: '—',
    resetStage: 0,
    resetTimer: null,
    toasts: [],
    
    // Overview data
    settings: { fencing_enabled: true, desired_workers: 3, outage_flaky_webhook: false, autoscale: 'manual' },
    kpis: { total: 0, processing: 0, retrying: 0, dlq: 0, succeeded: 0 },
    invariants: [],
    counters: {},
    breakers: [],
    workers: [],
    events: [],
    
    // Jobs & DLQ
    jobsList: [],
    jobFilterStatus: '',
    selectedJob: null,
    jobDrawerOpen: false,

    // Insights
    insightsData: { recommendation: {}, breakers: [], retry_stats: [] },

    // Chaos Lab
    chaosRunConfig: { jobs: 300, tenants: 3, jobs_per_sec: 15, kill_every_s: 8, freeze: true },
    chaosLatest: null,

    // Enqueue inline form
    newJob: { tenant_id: 'tenant-1', type: 'ledger_credit', payload: '{"amount": 100, "account": "ACC-101"}', idempotency_key: '', priority: 0 },

    init() {
      window.addEventListener('hashchange', () => {
        const hash = window.location.hash.substring(1);
        if (hash) this.activeTab = hash;
      });

      this.pollOverview();
      setInterval(() => this.pollOverview(), 1000);
      setInterval(() => {
        if (this.activeTab === 'jobs' || this.activeTab === 'dlq') this.fetchJobs();
        if (this.activeTab === 'insights') this.fetchInsights();
        if (this.activeTab === 'chaos') this.fetchChaosStatus();
      }, 2000);
    },

    setTab(tab) {
      this.activeTab = tab;
      window.location.hash = tab;
      if (tab === 'jobs' || tab === 'dlq') this.fetchJobs();
      if (tab === 'insights') this.fetchInsights();
    },

    toast(msg) {
      const id = Date.now();
      const time = new Date().toTimeString().split(' ')[0];
      this.toasts.push({ id, text: `${time}  ${msg}` });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, 4000);
    },

    async pollOverview() {
      try {
        const res = await fetch('/api/overview');
        if (!res.ok) throw new Error('API offline');
        const data = await res.json();
        this.connected = true;
        this.lastRefresh = new Date().toLocaleTimeString();
        this.settings = data.settings;
        this.kpis = data.kpis;
        this.invariants = data.invariants;
        this.counters = data.counters;
        this.breakers = data.breakers;
        this.workers = data.workers;
        this.events = data.events;
      } catch (err) {
        this.connected = false;
      }
    },

    async fetchJobs() {
      try {
        let url = '/api/jobs?limit=50';
        if (this.activeTab === 'dlq') {
          url += '&status=dead';
        } else if (this.jobFilterStatus) {
          url += `&status=${this.jobFilterStatus}`;
        }
        const res = await fetch(url);
        if (res.ok) {
          this.jobsList = await res.json();
        }
      } catch (err) {}
    },

    async openJobDetail(jobId) {
      try {
        const res = await fetch(`/api/jobs/${jobId}`);
        if (res.ok) {
          this.selectedJob = await res.json();
          this.jobDrawerOpen = true;
        }
      } catch (err) {
        this.toast(`Failed to load job ${jobId}`);
      }
    },

    async fetchInsights() {
      try {
        const res = await fetch('/api/insights');
        if (res.ok) {
          this.insightsData = await res.json();
        }
      } catch (err) {}
    },

    async fetchChaosStatus() {
      try {
        const res = await fetch('/api/chaos/run/latest');
        if (res.ok) {
          this.chaosLatest = await res.json();
        }
      } catch (err) {}
    },

    async killWorker(workerId) {
      await fetch(`/api/workers/${workerId}/kill`, { method: 'POST' });
      this.toast(`kill sent to ${workerId}`);
    },

    async freezeWorker(workerId) {
      await fetch(`/api/workers/${workerId}/freeze`, { method: 'POST' });
      this.toast(`freeze sent to ${workerId}`);
    },

    async scaleWorkers(delta) {
      const current = this.settings.desired_workers || 3;
      const desired = Math.max(1, Math.min(8, current + delta));
      await fetch('/api/workers/scale', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ desired })
      });
      this.toast(`scaled to ${desired} workers`);
    },

    async toggleFencing() {
      const next = !this.settings.fencing_enabled;
      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fencing_enabled: next })
      });
      this.toast(next ? 'Fencing ON (safe mode)' : 'Fencing OFF (unsafe mode)');
    },

    async toggleOutage() {
      const next = !this.settings.outage_flaky_webhook;
      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ outage_flaky_webhook: next })
      });
      this.toast(next ? 'Simulated outage: ON' : 'Simulated outage: OFF');
    },

    async injectPoison() {
      await fetch('/api/chaos/poison', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ count: 1 })
      });
      this.toast('1 poison job injected');
    },

    async replayJob(jobId) {
      const res = await fetch(`/api/jobs/${jobId}/replay`, { method: 'POST' });
      if (res.ok) {
        this.toast(`requeued ${jobId.substring(0, 8)}...`);
        this.fetchJobs();
      }
    },

    async replayAllDLQ() {
      const res = await fetch('/api/dlq/replay-all', { method: 'POST' });
      if (res.ok) {
        const d = await res.json();
        this.toast(`requeued ${d.replayed_count} dead jobs`);
        this.fetchJobs();
      }
    },

    async submitEnqueue() {
      let payloadObj = {};
      try {
        payloadObj = JSON.parse(this.newJob.payload);
      } catch (e) {
        this.toast('Invalid JSON payload');
        return;
      }

      const res = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tenant_id: this.newJob.tenant_id,
          type: this.newJob.type,
          payload: payloadObj,
          idempotency_key: this.newJob.idempotency_key || null,
          priority: parseInt(this.newJob.priority || 0)
        })
      });

      if (res.status === 201) {
        const j = await res.json();
        this.toast(`queued ${j.id.substring(0, 8)}...`);
        this.fetchJobs();
      } else if (res.status === 200) {
        const j = await res.json();
        this.toast(`duplicate: returned existing ${j.id.substring(0, 8)}...`);
      }
    },

    async runChaos() {
      await fetch('/api/chaos/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(this.chaosRunConfig)
      });
      this.toast('Chaos run started');
      this.fetchChaosStatus();
    },

    async stopChaos() {
      await fetch('/api/chaos/run/stop', { method: 'POST' });
      this.toast('Chaos stop requested');
    },

    handleResetClick() {
      if (this.resetStage === 0) {
        this.resetStage = 1;
        this.resetTimer = setTimeout(() => {
          this.resetStage = 0;
        }, 3000);
      } else {
        clearTimeout(this.resetTimer);
        this.resetStage = 0;
        this.executeReset();
      }
    },

    async executeReset() {
      const res = await fetch('/api/chaos/reset', { method: 'POST' });
      if (res.ok) {
        this.toast('Demo reset');
        this.pollOverview();
      } else {
        this.toast('Reset refused (LAZARUS_ALLOW_RESET disabled)');
      }
    }
  }));
});
