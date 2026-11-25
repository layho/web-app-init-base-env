# React module outlet (candidate)

Integrates the registration and routing helpers from #157 and #160 in one independent branch. ModuleOutlet uses React.lazy, Suspense and a per-module error boundary; it does not call a React component as a plain function. Explicit permission declarations filter the route before loading. This is UI visibility, not backend authorization. Static trusted code only; no remote module URLs or dynamic untrusted scripts. Error boundaries cover descendant rendering/lifecycle failures, not arbitrary event handlers or background work.

The example object-returning legacy module is a registry fixture, not a React page. The outlet is not yet connected to the production navigation. Browser tests for module switching, error recovery and hooks are required before enabling it. No dependency on #168 or changes to production databases. Do not merge independently of the final shell integration plan.
