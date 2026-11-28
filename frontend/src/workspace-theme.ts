export const workspaceTheme = {
  algorithm: 'dark' as const,
  headerHeight: 48,
  token: {
    colorPrimary: '#8ab4f8',
    colorBgLayout: '#0f1419',
    colorBgContainer: '#1b2129',
    colorText: '#e8eaed',
    colorTextSecondary: '#9aa0a6',
    colorBorder: '#3c4043',
    colorSuccess: '#81c995',
    colorWarning: '#fdd663',
    borderRadius: 8,
    fontFamily: '-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif',
  },
  components: {
    Layout: { siderBg: '#0f1419', headerBg: '#0f1419', bodyBg: '#0f1419', headerHeight: 48 },
    Menu: {
      darkItemBg: '#0f1419',
      darkItemColor: '#e8eaed',
      darkItemSelectedBg: '#30343a',
      darkItemSelectedColor: '#8ab4f8',
      darkItemHoverBg: '#282c31',
      itemBorderRadius: 8,
    },
    Card: { headerBg: '#1b2129' },
    Table: { headerBg: '#171c22', headerColor: '#e8eaed', colorBgContainer: '#1b2129' },
  },
}
