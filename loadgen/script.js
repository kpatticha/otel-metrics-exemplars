// k6 drives real HTTP traffic against checkout-service.
//
// The mix is weighted the way a storefront's would be: mostly browsing, some
// product detail views, a minority of orders, and a small share of malformed
// requests. Nothing here fabricates metrics -- it makes real requests, and the
// services measure themselves.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

const BASE = __ENV.CHECKOUT_BASE_URL || 'http://localhost:8000';
const VUS = parseInt(__ENV.K6_VUS || '10', 10);
const DURATION = __ENV.K6_DURATION || '30m';

// SKU-1005 is seeded with only 8 units, so ordering it in quantity genuinely
// exhausts the ledger and produces real 409s.
const SKUS = ['SKU-1001', 'SKU-1002', 'SKU-1003', 'SKU-1004', 'SKU-1005'];

export const orderSuccess = new Rate('order_success');

export const options = {
  scenarios: {
    storefront: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: '30s', target: Math.max(1, Math.floor(VUS / 2)) },
        { duration: '1m', target: VUS },
        { duration: DURATION, target: VUS },
        { duration: '30s', target: 1 },
      ],
      gracefulRampDown: '10s',
    },
  },
  // 409 and 400 are expected outcomes here, not failures, so the default
  // http_req_failed threshold would be misleading.
  thresholds: {
    'http_req_duration{expected_response:true}': ['p(95)<5000'],
  },
};

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

export default function () {
  const roll = Math.random();

  if (roll < 0.45) {
    // Browse. page_size varies, and larger pages genuinely cost more to
    // serialize downstream.
    const pageSize = 1 + Math.floor(Math.random() * 50);
    const res = http.get(`${BASE}/products?page_size=${pageSize}`, {
      tags: { endpoint: 'products' },
    });
    check(res, { 'browse ok': (r) => r.status === 200 });
  } else if (roll < 0.75) {
    // Product detail. Each SKU has its own key-derivation cost, so per-SKU
    // latency differs for a real reason.
    const res = http.get(`${BASE}/products/${pick(SKUS)}`, {
      tags: { endpoint: 'product' },
    });
    check(res, { 'detail ok': (r) => r.status === 200 });
  } else if (roll < 0.79) {
    // A genuinely malformed request -> real 400.
    const res = http.post(`${BASE}/orders`, JSON.stringify({ sku: 42, units: 'many' }), {
      headers: { 'Content-Type': 'application/json' },
      tags: { endpoint: 'orders_invalid' },
    });
    check(res, { 'invalid rejected': (r) => r.status === 400 });
  } else if (roll < 0.85) {
    // A SKU that does not exist -> real 404.
    const res = http.get(`${BASE}/products/SKU-DOES-NOT-EXIST`, {
      tags: { endpoint: 'product_missing' },
    });
    check(res, { 'missing sku 404': (r) => r.status === 404 });
  } else {
    // Order. Payment tokenization cost scales with units, so order latency
    // tracks order size.
    const units = 1 + Math.floor(Math.random() * 4);
    const res = http.post(
      `${BASE}/orders`,
      JSON.stringify({ sku: pick(SKUS), units }),
      {
        headers: { 'Content-Type': 'application/json' },
        tags: { endpoint: 'orders' },
      }
    );
    orderSuccess.add(res.status === 200);
    check(res, { 'order handled': (r) => [200, 409].includes(r.status) });
  }

  sleep(0.2 + Math.random() * 0.8);
}
