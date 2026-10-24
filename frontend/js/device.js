import { html, raw, esc, tag } from './core.js';

export async function deviceReport() {
  const r = { webgl: !!document.createElement('canvas').getContext('webgl2'), secure: window.isSecureContext, xrApi: !!navigator.xr, ar: false, geo: !!navigator.geolocation, camera: !!navigator.mediaDevices?.getUserMedia, motion: 'DeviceOrientationEvent' in window };
  if (navigator.xr) { try { r.ar = await navigator.xr.isSessionSupported('immersive-ar'); } catch { r.ar = false; } }
  return r;
}
export const deviceHtml = (r) => html`<table class="t"><tbody>${[['3D rendering (WebGL2)', r.webgl, 'Required'], ['Secure context (HTTPS or localhost)', r.secure, 'Required for camera, sensors and AR'], ['WebXR immersive AR', r.ar, r.xrApi ? 'Needs an ARCore device with Chrome (Android)' : 'Not exposed by this browser; iPhone/iPad Safari does not support WebXR AR'], ['Camera', r.camera, 'Needed for guided navigation overlay'], ['Location', r.geo, 'Needed for navigation'], ['Motion/compass sensors', r.motion, 'Needed for heading']].map(([k, ok, note]) => raw(`<tr><td>${esc(k)}</td><td>${ok ? tag('available', 'ready').s : tag('unavailable', 'neutral').s}</td><td class="help">${esc(note)}</td></tr>`))}</tbody></table>`;
