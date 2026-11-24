// k6 load/fault drill for MarketLens.
//
//   BASE_URL=https://api.example.com K6_TOKEN="$(cat token)" k6 run deploy/load/k6-smoke.js
//
// Health checks are always executed. Authenticated read paths run only when a
// token is supplied. Use a dedicated test tenant; never point this at
// production customer data without an approved drill window.
import http from 'k6/http';
import { check, sleep } from 'k6';

const base = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const token = __ENV.K6_TOKEN || '';
const headers = token ? { Authorization: `Bearer ${token}` } : {};

export const options = {
  scenarios: {
    smoke: {
      executor: 'constant-vus',
      vus: Number(__ENV.VUS || 5),
      duration: __ENV.DURATION || '2m',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{expected_response:true}': ['p(95)<500'],
  },
};

export default function () {
  const live = http.get(`${base}/health/live`);
  check(live, { 'live is 200': (r) => r.status === 200 });

  const ready = http.get(`${base}/health/ready`);
  check(ready, { 'ready is 200': (r) => r.status === 200 });

  if (token) {
    const projects = http.get(`${base}/api/v1/projects`, { headers });
    check(projects, { 'projects is 2xx/4xx-not-5xx': (r) => r.status < 500 });
  }
  sleep(1);
}
