import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { lucideCopy, lucideLogOut, lucideUser } from '@ng-icons/lucide';

import { HlmButton } from '@spartan-ng/helm/button';
import { HlmSpinner } from '@spartan-ng/helm/spinner';
import {
  HlmDropdownMenu,
  HlmDropdownMenuItem,
  HlmDropdownMenuLabel,
  HlmDropdownMenuSeparator,
  HlmDropdownMenuTrigger,
} from '@spartan-ng/helm/dropdown-menu';

import { AuthService } from '../core/auth.service';

/**
 * Header user badge.
 *
 * - Anonymous: a neutral pill reading "Anonymous"; clicking starts the OIDC
 *   login redirect.
 * - Authenticating: a disabled pill with a spinner.
 * - Authenticated: an avatar (initials) + display name; clicking opens a menu
 *   with the user's email, "Copy access token" and "Sign out".
 */
@Component({
  selector: 'app-user-badge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    HlmButton,
    HlmSpinner,
    HlmDropdownMenu,
    HlmDropdownMenuItem,
    HlmDropdownMenuLabel,
    HlmDropdownMenuSeparator,
    HlmDropdownMenuTrigger,
  ],
  providers: [provideIcons({ lucideUser, lucideCopy, lucideLogOut })],
  template: `
    @if (status() === 'authenticating') {
      <span
        class="inline-flex items-center gap-2 rounded-full border border-zinc-200 bg-white px-3 py-1.5 text-sm font-medium text-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-400"
      >
        <hlm-spinner class="size-4" />
        <span class="hidden sm:inline">Signing in…</span>
      </span>
    } @else if (status() !== 'authenticated' || !user()) {
      <button
        hlmBtn
        variant="outline"
        size="sm"
        class="gap-2 rounded-full"
        [title]="error() ?? 'Sign in'"
        aria-label="Sign in"
        (click)="auth.login()"
      >
        <span
          class="flex size-6 items-center justify-center rounded-full bg-zinc-200 text-zinc-500 dark:bg-zinc-700 dark:text-zinc-300"
        >
          <ng-icon name="lucideUser" size="1rem" />
        </span>
        <span>Anonymous</span>
      </button>
    } @else {
      <button
        [hlmDropdownMenuTrigger]="menu"
        align="end"
        hlmBtn
        variant="outline"
        size="sm"
        class="gap-2 rounded-full ps-1 pe-2"
        [title]="user()!.displayName"
      >
        <span
          class="flex size-7 items-center justify-center rounded-full bg-blue-600 text-xs font-semibold text-white"
        >
          {{ user()!.initials }}
        </span>
        <span class="hidden max-w-[12rem] truncate sm:inline">{{ user()!.displayName }}</span>
      </button>

      <ng-template #menu>
        <hlm-dropdown-menu class="w-64">
          <hlm-dropdown-menu-label>
            <p class="truncate text-sm font-semibold">{{ user()!.displayName }}</p>
            @if (user()!.email) {
              <p class="truncate text-xs font-normal text-muted-foreground">{{ user()!.email }}</p>
            }
          </hlm-dropdown-menu-label>
          <hlm-dropdown-menu-separator />
          <button hlmDropdownMenuItem (click)="onCopyToken()">
            <ng-icon name="lucideCopy" size="1rem" />
            {{ copied() ? 'Copied!' : 'Copy access token' }}
          </button>
          <button hlmDropdownMenuItem (click)="auth.logout()">
            <ng-icon name="lucideLogOut" size="1rem" />
            Sign out
          </button>
        </hlm-dropdown-menu>
      </ng-template>
    }
  `,
})
export class UserBadge {
  protected readonly auth = inject(AuthService);
  protected readonly status = this.auth.status;
  protected readonly user = this.auth.user;
  protected readonly error = this.auth.error;
  protected readonly copied = signal(false);

  protected async onCopyToken(): Promise<void> {
    const ok = await this.auth.copyAccessToken();
    this.copied.set(ok);
    if (ok) {
      setTimeout(() => this.copied.set(false), 1500);
    }
  }
}
