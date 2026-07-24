import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { AppLayout } from './layout/app-layout';
import { AuthService } from './core/auth.service';

@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AppLayout],
  template: `<app-layout />`,
})
export class App {
  // Instantiate the auth service eagerly so it can complete an OIDC redirect
  // callback / silently restore a stored session on app load.
  private readonly auth = inject(AuthService);

  constructor() {
    void this.auth.bootstrap();
  }
}
