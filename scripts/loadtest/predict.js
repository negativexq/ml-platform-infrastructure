import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:30080";

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-vus",
      startVUs: 2,
      stages: [
        { duration: "20s", target: 60 },
        { duration: "3m", target: 60 },
        { duration: "20s", target: 0 },
      ],
      gracefulRampDown: "10s",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
  },
};

const payload = JSON.stringify({ features: [1.0, 2.0, 3.0] });
const params = { headers: { "Content-Type": "application/json" } };

export default function () {
  const res = http.post(`${BASE_URL}/predict`, payload, params);
  check(res, { "status is 200": (r) => r.status === 200 });
}
