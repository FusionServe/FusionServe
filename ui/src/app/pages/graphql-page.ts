import {
  ChangeDetectionStrategy,
  Component,
  DOCUMENT,
  effect,
  inject,
  signal,
} from '@angular/core';
import { DomSanitizer, type SafeHtml } from '@angular/platform-browser';

import { AuthService } from '../core/auth.service';
import { ConfigService } from '../core/config.service';

/**
 * GraphQL playground page.
 *
 * Embeds the Altair GraphQL IDE, replacing the previous GraphiQL viewer.
 * Altair ships as a self-contained static Angular app; its ``build/dist`` is
 * copied into the bundle under ``assets/altair`` (see ``angular.json``). Rather
 * than importing ``altair-static`` (a Node-only package that references
 * ``fs``/``path`` and can't be bundled for the browser), we fetch its
 * ``index.html`` at runtime, point its ``<base href>`` at the copied assets,
 * and inject the documented ``AltairGraphQL.init(...)`` bootstrap snippet — all
 * inside a sandboxed iframe via ``srcdoc``.
 *
 * The GraphQL endpoint comes from the runtime config; when the user is signed
 * in the current access token is passed as an ``Authorization`` header so
 * PostgreSQL row-level security applies the authenticated role. The iframe is
 * rebuilt whenever the auth status changes (sign-in / sign-out / renewal) so
 * the IDE always carries the current credential.
 */
@Component({
  selector: 'app-graphql',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block h-full' },
  template: `
    <div class="h-full overflow-hidden rounded-lg border border-zinc-200 dark:border-zinc-800">
      @if (error(); as err) {
        <div class="flex h-full items-center justify-center p-4 text-sm text-red-600 dark:text-red-400">
          {{ err }}
        </div>
      } @else if (srcdoc(); as doc) {
        <iframe title="Altair GraphQL IDE" class="h-full w-full border-0" [srcdoc]="doc"></iframe>
      } @else {
        <div class="flex h-full items-center justify-center text-sm text-zinc-500 dark:text-zinc-400">
          Loading GraphQL IDE…
        </div>
      }
    </div>
  `,
})
export class GraphqlPage {
  private readonly config = inject(ConfigService);
  private readonly auth = inject(AuthService);
  private readonly sanitizer = inject(DomSanitizer);
  private readonly document = inject(DOCUMENT);

  protected readonly srcdoc = signal<SafeHtml | null>(null);
  protected readonly error = signal<string | null>(null);

  /** Cached Altair ``index.html`` template (fetched once). */
  private templatePromise: Promise<string> | null = null;

  constructor() {
    // Rebuild the embedded IDE whenever the auth status changes so the
    // injected ``Authorization`` header reflects the current session.
    effect(() => {
      const status = this.auth.status();
      void this.render(status);
    });
  }

  private loadTemplate(baseUrl: string): Promise<string> {
    if (!this.templatePromise) {
      this.templatePromise = fetch(new URL('index.html', baseUrl).href).then((r) => {
        if (!r.ok) throw new Error(`Altair assets responded ${r.status}`);
        return r.text();
      });
    }
    return this.templatePromise;
  }

  private async render(_status: string): Promise<void> {
    try {
      const cfg = await this.config.ensureConfig();
      // Absolute base for Altair's own chunk/asset URLs (copied to assets/altair).
      const baseUrl = new URL('altair/', this.document.baseURI).href;
      const template = await this.loadTemplate(baseUrl);

      const token = this.auth.getAccessToken();
      const options: Record<string, unknown> = { endpointURL: cfg.graphqlUrl };
      if (token) options['initialHeaders'] = { Authorization: `Bearer ${token}` };

      const initSnippet =
        `<script type="module">AltairGraphQL.init(${JSON.stringify(options)});<` + `/script>`;

      // Point the app at its copied assets and inject the bootstrap snippet.
      const html = template
        .replace(/<base\b[^>]*>/i, `<base href="${baseUrl}" />`)
        .replace('</body>', `${initSnippet}</body>`);

      this.srcdoc.set(this.sanitizer.bypassSecurityTrustHtml(html));
      this.error.set(null);
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : String(e));
    }
  }
}
