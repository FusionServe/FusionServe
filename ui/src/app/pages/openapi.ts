import {
  CUSTOM_ELEMENTS_SCHEMA,
  ChangeDetectionStrategy,
  Component,
  DOCUMENT,
  OnInit,
  inject,
  signal,
} from '@angular/core';

// Registers the ``<elements-api>`` custom element. Statically imported so it
// lands in this lazily-loaded route chunk (not the initial bundle).
import '@stoplight/elements/web-components.min.js';

import { ConfigService } from '../core/config.service';

/**
 * OpenAPI reference page.
 *
 * Renders the auto-generated OpenAPI 3.1 document with Stoplight Elements
 * (``<elements-api>``), replacing the previous ``swagger-ui-react`` viewer. The
 * spec URL is resolved from the runtime config; in dev the Angular dev server
 * proxies it (and "Try it" requests) to the Litestar backend.
 *
 * Stoplight ships a single stylesheet that is copied into the build under
 * ``assets/vendor/stoplight`` (see ``angular.json``) and injected as a
 * ``<link>`` on init so it only loads with this lazy route.
 */
@Component({
  selector: 'app-openapi',
  changeDetection: ChangeDetectionStrategy.OnPush,
  schemas: [CUSTOM_ELEMENTS_SCHEMA],
  host: { class: 'block h-full' },
  template: `
    <div
      class="h-full overflow-hidden rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950"
    >
      @if (openapiUrl(); as url) {
        <elements-api [attr.apiDescriptionUrl]="url" router="hash" layout="sidebar"></elements-api>
      } @else {
        <div class="flex h-full items-center justify-center text-sm text-zinc-500 dark:text-zinc-400">
          Loading…
        </div>
      }
    </div>
  `,
})
export class OpenApiPage implements OnInit {
  private readonly config = inject(ConfigService);
  private readonly document = inject(DOCUMENT);

  protected readonly openapiUrl = signal<string | null>(null);

  ngOnInit(): void {
    this.ensureStylesheet();
    void this.config.ensureConfig().then((cfg) => this.openapiUrl.set(cfg.openapiUrl));
  }

  /** Inject the Stoplight stylesheet once (relative to the document base href). */
  private ensureStylesheet(): void {
    const id = 'stoplight-elements-styles';
    if (this.document.getElementById(id)) return;
    const link = this.document.createElement('link');
    link.id = id;
    link.rel = 'stylesheet';
    link.href = 'vendor/stoplight/styles.min.css';
    this.document.head.appendChild(link);
  }
}
