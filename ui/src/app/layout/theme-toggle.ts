import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { lucideMonitor, lucideMoon, lucideSun } from '@ng-icons/lucide';

import { HlmButton } from '@spartan-ng/helm/button';
import { type Theme, ThemeService } from '../core/theme.service';

// Order the cycle button steps through on each click.
const ORDER: readonly Theme[] = ['light', 'dark', 'system'];

const LABELS: Record<Theme, string> = {
  light: 'Light',
  dark: 'Dark',
  system: 'System',
};

const ICONS: Record<Theme, string> = {
  light: 'lucideSun',
  dark: 'lucideMoon',
  system: 'lucideMonitor',
};

/**
 * Cycle button for the colour theme: Light -> Dark -> System.
 *
 * The displayed icon and label reflect the current preference; clicking
 * advances to the next mode and persists it via {@link ThemeService}.
 */
@Component({
  selector: 'app-theme-toggle',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, HlmButton],
  providers: [provideIcons({ lucideSun, lucideMoon, lucideMonitor })],
  template: `
    <button
      hlmBtn
      variant="outline"
      size="sm"
      class="gap-2 rounded-full"
      [title]="'Theme: ' + label() + ' (click for ' + nextLabel() + ')'"
      [attr.aria-label]="'Theme: ' + label() + '. Switch to ' + nextLabel() + '.'"
      (click)="cycle()"
    >
      <ng-icon [name]="icon()" size="1rem" />
      <span class="hidden sm:inline">{{ label() }}</span>
    </button>
  `,
})
export class ThemeToggle {
  private readonly themeService = inject(ThemeService);

  protected readonly theme = this.themeService.theme;
  protected readonly label = computed(() => LABELS[this.theme()]);
  protected readonly icon = computed(() => ICONS[this.theme()]);
  protected readonly next = computed(
    () => ORDER[(ORDER.indexOf(this.theme()) + 1) % ORDER.length],
  );
  protected readonly nextLabel = computed(() => LABELS[this.next()]);

  protected cycle(): void {
    this.themeService.setTheme(this.next());
  }
}
