"""Bounded local human handoff; no credentials, page text or model calls."""
import time
from .computer import ComputerError, write_new


class ActiveClock:
    def __init__(self, wall=time.monotonic):
        self.wall, self.excluded, self.paused = wall, 0.0, None

    def __call__(self):
        return (self.wall() if self.paused is None else self.paused) - self.excluded

    def pause(self):
        if self.paused is not None:
            raise ComputerError('human_wait_already_active')
        self.paused = self.wall()

    def resume(self):
        if self.paused is not None:
            self.excluded += self.wall() - self.paused
            self.paused = None

    def wall_deadline(self, active_deadline):
        return self.wall() + max(0, active_deadline - self())


class HumanHandoff:
    MAX_HANDOFFS, MAX_WAIT = 3, 900

    def __init__(self, run, config, *, stop, clock, callback=None, wall=time.monotonic):
        self.run, self.config, self.stop = run, config, stop
        self.clock, self.callback, self.wall = clock, callback, wall
        self.count = 0

    def perform(self, browser):
        if self.config.get('mode') != 'github_repo' or self.callback is None:
            raise ComputerError('browser_human_required')
        if self.count >= self.MAX_HANDOFFS:
            raise ComputerError('browser_handoff_limit')
        self.count += 1
        request = {'site': 'github.com', 'target_url': self.config['start_url'],
                   'reason': 'login_or_verification', 'handoff': self.count}
        prefix = self.run / f'human-{self.count:02}'
        write_new(prefix.with_suffix('.request.json'), request)
        state = 'failed'
        try:
            if self.stop():
                raise ComputerError('goal_cancelled')
            browser.enter_manual()
            until = self.wall() + self.MAX_WAIT
            def tick():
                if self.stop():
                    raise ComputerError('goal_cancelled')
                if self.wall() >= until:
                    raise ComputerError('browser_human_timeout')
                # The browser returns a fixed status, never login DOM/headers.
                browser.human_tick(wall_deadline=until)
                if self.stop():
                    raise ComputerError('goal_cancelled')
                if self.wall() >= until:
                    raise ComputerError('browser_human_timeout')
            self.clock.pause()
            try:
                accepted = self.callback(dict(request), tick)
            finally:
                self.clock.resume()
            if self.stop():
                raise ComputerError('goal_cancelled')
            if self.wall() >= until:
                raise ComputerError('browser_human_timeout')
            if accepted is not True:
                raise ComputerError('browser_human_declined')
            # A click on Continue grants reobservation, not authentication proof.
            browser.resume_manual()
            state = 'reobserve'
        finally:
            self.clock.resume()
            write_new(prefix.with_suffix('.result.json'), {'state': state, 'handoff': self.count})
