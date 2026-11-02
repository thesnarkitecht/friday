import {App, Toast} from '@wearables-ui-toolkit/mrbd';
import {ReactRouterNavigationProvider, ReactRouterPageTransition} from '@wearables-ui-toolkit/mrbd/react-router';
import bellRingFilled from '@wearables-ui-toolkit/icons/svg/bellring__filled.svg';
import {useEffect} from 'react';
import {HashRouter, Navigate, Route, Routes, useNavigate} from 'react-router-dom';
import {setIncomingHandler} from './link';
import {AlertsPage} from './pages/AlertsPage';
import {HomePage} from './pages/HomePage';
import {MemoryPage} from './pages/MemoryPage';
import {ScreenPage} from './pages/ScreenPage';
import {TalkPage} from './pages/TalkPage';

/** Alerts and reminders surface as toasts wherever you are; screen sharing jumps to the mirror. */
function LinkEffects() {
  const navigate = useNavigate();
  useEffect(() => {
    setIncomingHandler(msg => {
      if (msg.type === 'notify') Toast.show(msg.text, msg.kind, bellRingFilled);
      if (msg.type === 'screen') navigate(msg.frame ? '/screen' : '/', {replace: msg.frame == null});
    });
  }, [navigate]);
  return null;
}

export function FridayGlassesApp() {
  return (
    <HashRouter>
      <ReactRouterNavigationProvider>
        <App>
          <LinkEffects />
          <ReactRouterPageTransition>
            {({location}) => (
              <Routes location={location}>
                <Route path="/" element={<HomePage />} />
                <Route path="/talk" element={<TalkPage />} />
                <Route path="/memory" element={<MemoryPage />} />
                <Route path="/alerts" element={<AlertsPage />} />
                <Route path="/screen" element={<ScreenPage />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            )}
          </ReactRouterPageTransition>
        </App>
      </ReactRouterNavigationProvider>
    </HashRouter>
  );
}
