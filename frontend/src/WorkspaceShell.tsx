import { useState } from 'react'
import type { ReactNode } from 'react'
import { LogoutOutlined } from '@ant-design/icons'
import { Button, Layout, Menu, Space, Typography } from 'antd'
import './workspace.css'

const { Header, Sider, Content } = Layout

type NavigationItem<Key extends string> = {
  key: Key
  label: string
  icon: ReactNode
}

type WorkspaceShellProps<Key extends string> = {
  appName: string
  brandMark: string
  username: string
  activePage: Key
  navigation: readonly NavigationItem<Key>[]
  onNavigate: (key: Key) => void
  onLogout: () => void
  logoutDisabled: boolean
  headerActions?: ReactNode
  children: ReactNode
}

export function WorkspaceShell<Key extends string>({
  appName, brandMark, username, activePage, navigation, onNavigate, onLogout, logoutDisabled, headerActions, children,
}: WorkspaceShellProps<Key>) {
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 767px)').matches)
  const pageTitle = navigation.find(item => item.key === activePage)?.label

  return (
    <Layout className="workspace-shell" style={{ flexDirection: 'column' }}>
        <Header className="workspace-header">
          <div className="workspace-brand" aria-label={appName}>
            <span className="workspace-mark" aria-hidden="true">{brandMark}</span>
            <span className="workspace-name">{appName}</span>
          </div>
          <Space size="small" wrap className="workspace-header-actions">
            {headerActions}
            <Typography.Text className="workspace-user">{username}</Typography.Text>
            <Button icon={<LogoutOutlined />} onClick={onLogout} disabled={logoutDisabled}>退出登录</Button>
          </Space>
        </Header>
      <Layout className="workspace-frame">
      <Sider
        className="workspace-sider"
        theme="light"
        width={228}
        collapsedWidth={76}
        breakpoint="md"
        collapsed={narrow}
        onBreakpoint={setNarrow}
        trigger={null}
      >
        <nav aria-label="主导航">
          <Menu
            className="workspace-menu"
            mode="inline"
            inlineCollapsed={narrow}
            selectedKeys={[activePage]}
            items={navigation.map(item => ({ ...item, title: item.label }))}
            onClick={({ key }) => {
              const item = navigation.find(page => page.key === key)
              if (item) onNavigate(item.key)
            }}
          />
        </nav>
      </Sider>
      <Layout className="workspace-body">
        <Content>
          <main className="workspace-content workspace-content-responsive" aria-label={pageTitle}>
            {children}
          </main>
        </Content>
      </Layout>
      </Layout>
    </Layout>
  )
}
