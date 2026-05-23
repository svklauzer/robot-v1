import "./globals.css";

export const metadata = {
  title: "Robot V1 Owner Panel",
  description: "Trading Robot Owner Dashboard"
};

export default function RootLayout({
  children
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}